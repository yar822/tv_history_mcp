from __future__ import annotations

import math
import time
from threading import RLock
from copy import deepcopy
import json
from zipfile import ZipFile, ZIP_DEFLATED

import pandas as pd

from .config import Settings
from .provider import HistoryProvider
from .storage import CsvStorage, DownloadControlLog, atomic_json, read_json
from .provider import split_asset
from .calendar import learn_calendar, suspend_conflicts
from .metadata import compact_calendar, expand_calendar
from .market_rules import resolve_market_rule
from .coverage import audit_gaps, window_coverage
from .calendar import slot, boundary_from_rule
from .trading_dates import normalize_daily, normalize_weekly, uses_trading_dates


TIMEFRAME_DURATIONS = {
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
    "1D": pd.Timedelta(days=1),
    "1W": pd.Timedelta(days=7),
}

STARTUP_CHECK_VERSION = 1
STARTUP_CHECK_DAYS = 100
REFRESH_REUSE_SECONDS = 10.0


def current_utc_time():
    return pd.Timestamp.now(tz="UTC")


class HistorySynchronizer:
    def __init__(self, settings: Settings, provider: HistoryProvider, storage: CsvStorage, *, refresh_on_start=False):
        self.settings = settings
        self.provider = provider
        self.storage = storage
        self.control_log = DownloadControlLog(settings.data_root)
        self._lock = RLock()
        self.calendars = {}
        self._initialized = {}
        self.coverage = {}
        self._last_fetch = {}
        self._successful_refresh_at = {}
        self._metadata_checked = set()
        if refresh_on_start:
            self.backup_path = self._backup_cache()
        known = set((settings.timestamp_profiles or {}).keys())
        known.update(settings.rus_daily_trading_date_assets)
        known.update((settings.calendar_timezones or {}).keys())
        for exchange, rule in (settings.market_rules or {}).items():
            known.update(f"{exchange}:{symbol}" for symbol in rule.get("symbols", {}))
        registry_path = settings.data_root / "ticker_registry.json"
        registry = read_json(registry_path, {})
        known.update(registry)
        if self.control_log.path.exists():
            known.update(pd.read_csv(self.control_log.path)["asset"].dropna())
        for name in sorted({self._canonical(n) for n in known}):
            asset = self._canonical(name)
            folder = self.storage.path_for(asset).parent
            self.storage.migrate_metadata(asset)
            self.coverage[asset] = self.storage.read_coverage(asset)
            if refresh_on_start:
                self.get_provider_metadata(asset)
            signature = self._cache_signature(asset)
            rules = self._check_settings(asset)
            previous = self.coverage[asset].get("startup_check", {})
            calendar = expand_calendar(read_json(folder / "calendar.json", {}))
            if (refresh_on_start and calendar and previous
                    and previous.get("files") == signature
                    and previous.get("settings") == rules
                    and previous.get("calendar_file") == self._file_signature(folder / "calendar.json")
                    and all(tf in self.coverage[asset] for tf in TIMEFRAME_DURATIONS)):
                self.calendars[asset] = calendar
                self._initialized[asset] = previous["present_timeframes"]
                self._save_calendar(asset)
                previous["calendar_file"] = self._file_signature(folder / "calendar.json")
                self._save_coverage(asset)
                continue
            frames = self._frames(asset)
            # Migrate existing caches; only missing timeframes need initialization.
            present = [tf for tf, frame in frames.items() if not frame.empty]
            if not present:
                continue
            self._initialized[asset] = present
            self._build_calendar(asset, frames)
            if refresh_on_start:
                self._check_startup(asset, frames, signature, rules)
            else:
                self._audit(asset, frames)
        self._persist_registry()

    @staticmethod
    def _file_signature(path):
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        return {"size": info.st_size, "mtime_ns": info.st_mtime_ns}

    def get_provider_metadata(self, asset):
        """One saved observation per ticker per process, shared by all callers."""
        from .provider_metadata import unknown_metadata

        asset = self._canonical(asset)
        with self._lock:
            state = self.coverage.setdefault(asset, {})
            if asset not in self._metadata_checked:
                metadata = unknown_metadata()
                try:
                    getter = getattr(self.provider, "get_metadata", None)
                    if getter is not None:
                        metadata = getter(asset)
                except Exception:
                    pass
                state["provider_metadata"] = metadata
                self._metadata_checked.add(asset)
                self._save_coverage(asset)
            return deepcopy(state["provider_metadata"])

    def _cache_signature(self, asset):
        return {tf: self._file_signature(self.storage.path_for(asset, tf))
                for tf in TIMEFRAME_DURATIONS}

    def _check_settings(self, asset):
        rule = resolve_market_rule(self.settings, asset)
        return {"version": STARTUP_CHECK_VERSION, "days": STARTUP_CHECK_DAYS,
                "timezone": rule.timezone, "timestamp_profile": rule.timestamp_profile,
                "sessions": self.settings.calendar_sessions,
                "lookback_days": self.settings.calendar_lookback_days,
                "min_observations": self.settings.calendar_min_observations,
                "min_agreement": self.settings.calendar_min_agreement,
                "boundary_overrides": (self.settings.calendar_boundary_overrides or {}).get(asset, {})}

    def _check_startup(self, asset, frames, signature, rules):
        end = max(f.index.max() for f in frames.values() if not f.empty)
        start = end - pd.Timedelta(days=STARTUP_CHECK_DAYS)
        self._audit(asset, frames, gap_start=start)
        repaired = {}
        for tf in TIMEFRAME_DURATIONS:
            state = self.coverage[asset][tf]
            keys = {self._gap_key(g) for g in state["unresolved_gaps"]
                    if pd.Timestamp(g["before"]) > start}
            attempted = set(state.get("attempted_gaps", []))
            if not keys - attempted:
                continue
            try:
                self._fetch(asset, tf, current_utc_time(), self.settings.initial_bars)
            except Exception as exc:
                state["refresh_error"] = str(exc)
            # Remember failed as well as unresolved attempts; a process restart
            # alone is not a reason to repeat the same provider request.
            state["attempted_gaps"] = sorted(attempted | keys)
            repaired[tf] = keys
        if repaired:
            after_repair = self._cache_signature(asset)
            if after_repair != signature:
                signature = after_repair
                frames = self._frames(asset)
                self._build_calendar(asset, frames)
            end = max(f.index.max() for f in frames.values() if not f.empty)
            start = end - pd.Timedelta(days=STARTUP_CHECK_DAYS)
            self._audit(asset, frames, gap_start=start)
            for tf in repaired:
                state = self.coverage[asset][tf]
                state["attempted_gaps"] = sorted(set(state["attempted_gaps"]) | {
                    self._gap_key(g) for g in state["unresolved_gaps"]
                    if pd.Timestamp(g["before"]) > start})
        self.coverage[asset].pop("startup_check", None)
        # Do not certify files changed by another writer while being checked.
        if signature == self._cache_signature(asset):
            self.coverage[asset]["startup_check"] = {
                "files": signature, "settings": rules,
                "calendar_file": self._file_signature(self.storage.path_for(asset).parent / "calendar.json"),
                "present_timeframes": self._initialized[asset],
                "checked_at": current_utc_time().isoformat(),
                "from": start.isoformat(), "through": end.isoformat()}
        self._save_coverage(asset)

    @staticmethod
    def _canonical(asset):
        symbol, exchange = split_asset(asset)
        return f"{exchange}:{symbol}"

    def _backup_cache(self):
        root = self.settings.data_root
        folder = root / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (pd.Timestamp.now(tz="UTC").strftime("%Y%m%d_%H%M%S_%f") + ".zip")
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            # Only cache folders, never recursively include previous backups.
            parents = {p.parent for tf in TIMEFRAME_DURATIONS for p in root.glob(f"*/*/{tf}.csv")}
            files = [p for parent in parents for p in parent.iterdir() if p.is_file()]
            files += [p for p in root.iterdir() if p.is_file()]
            for p in files:
                archive.write(p, p.relative_to(root))
        return str(path)

    def _frames(self, asset):
        return {tf: self._read_prices(asset, tf) for tf in TIMEFRAME_DURATIONS}

    def _read_prices(self, asset, timeframe):
        frame = self.storage.read(asset, timeframe)
        frame.attrs = {}
        return frame

    def _persist_registry(self):
        atomic_json(self.settings.data_root / "ticker_registry.json", self._initialized)

    def _save_coverage(self, asset):
        self.storage.save_coverage(asset, self.coverage[asset])

    @staticmethod
    def _gap_key(gap):
        return f"tail|{gap['after']}" if gap.get("terminal") else f"{gap['after']}|{gap['before']}"

    def _audit(self, asset, frames=None, mark_attempted=False, gap_start=None):
        frames = frames if frames is not None else self._frames(asset)
        checked = frames
        if gap_start is not None:
            # Retain the preceding bar so a hole crossing the window's start
            # cannot disappear when slicing. Other timeframes provide evidence.
            checked = {tf: f.iloc[max(0, f.index.searchsorted(gap_start) - 1):]
                       for tf, f in frames.items()}
            cached_end = max(f.index.max() for f in frames.values() if not f.empty)
        states = self.coverage.setdefault(asset, {})
        for tf in TIMEFRAME_DURATIONS:
            state = states.setdefault(tf, {})
            gaps = audit_gaps(asset, tf, checked, self.calendars.get(asset))
            if gap_start is not None:
                gaps = [g for g in gaps if pd.Timestamp(g["before"]) > gap_start]
                gaps = [g for g in state.get("unresolved_gaps", [])
                        if pd.Timestamp(g["before"]) <= gap_start] + gaps
                state["audit_from"] = gap_start.isoformat()
            else:
                state.pop("audit_from", None)
            state["unresolved_gaps"] = gaps
            state["timeframe"] = tf
            state["raw_last"] = frames[tf].index.max().isoformat() if not frames[tf].empty else None
            state["tail_evidence"] = []
            state.pop("next_session_open", None)
            if state["raw_last"]:
                last = frames[tf].index.max()
                cal = self.calendars.get(asset, {})
                if cal.get("status") == "ready":
                    key = f"{tf}|{slot(last, cal['timezone'])}"
                    rule = cal.get("rules", {}).get(key)
                    if rule and key not in cal.get("suspended_rules", {}):
                        try:
                            state["next_session_open"] = boundary_from_rule(last, rule["next_offset"], cal["timezone"]).isoformat()
                        except ValueError:
                            pass
                if tf in {"1h", "4h"}:
                    for other_tf, other in frames.items():
                        if other_tf == tf or other_tf in {"1D", "1W"}:
                            continue
                        pos = other.index.searchsorted(last + TIMEFRAME_DURATIONS[tf])
                        if pos < len(other):
                            state["tail_evidence"].append((other.index[pos] + TIMEFRAME_DURATIONS[other_tf]).isoformat())
            if gap_start is not None and tf in {"1h", "4h"} and not frames[tf].empty:
                # Check only to cached evidence, never to wall-clock now. A
                # shorter tail is repairable only if another intraday series
                # proves activity after it; staleness alone remains untouched.
                raw = frames[tf].copy(deep=False)
                raw.attrs = {"history_coverage": state}
                quality = window_coverage(raw, cached_end, gap_start)
                state["unresolved_gaps"].extend(
                    g for g in quality["unresolved_gaps"]
                    if g.get("terminal") and g["status"] == "incomplete")
            if mark_attempted:
                state["attempted_gaps"] = [self._gap_key(g) for g in state["unresolved_gaps"]]
        self._save_coverage(asset)

    def _save_calendar(self, asset):
        path = self.storage.path_for(asset).parent / "calendar.json"
        document = compact_calendar(self.calendars[asset])
        if read_json(path, {}) != document:
            atomic_json(path, document)

    def _build_calendar(self, asset, frames):
        calendar = learn_calendar(asset, frames, self.settings, pd.Timestamp.now(tz="UTC"))
        self.calendars[asset] = calendar
        self._save_calendar(asset)

    def _initialize(self, asset, requested_at):
        present = self._initialized.setdefault(asset, [])
        changed = False
        for tf in TIMEFRAME_DURATIONS:
            if tf in present:
                continue
            self._fetch(asset, tf, requested_at, self.settings.initial_bars)
            present.append(tf)
            changed = True
            self._persist_registry()
        if changed or asset not in self.calendars:
            self._build_calendar(asset, self._frames(asset))
            self._audit(asset, mark_attempted=True)
        return changed

    def ensure_available(
        self, asset: str, timeframe: str, requested_at: pd.Timestamp,
        *, count=None, sessions=None, required_start=None,
    ) -> tuple[pd.DataFrame, dict]:
        # One initializer/refresh per ticker at a time. Provider already serializes
        # websocket downloads; this also prevents older responses overwriting newer ones.
        with self._lock:
            return self._ensure_available(self._canonical(asset), timeframe, requested_at,
                                          count=count, sessions=sessions, required_start=required_start)

    def _ensure_available(self, asset, timeframe, requested_at, *, count=None, sessions=None, required_start=None):
        if timeframe not in TIMEFRAME_DURATIONS:
            raise ValueError("timeframe must be one of: 1h, 4h, 1D, 1W")
        initialized_now = self._initialize(asset, requested_at)
        stored = self.storage.read(asset, timeframe)
        meta = {"refreshed": initialized_now, "reason": "initial_load" if initialized_now else "cache_checked",
                "bars_requested": self.settings.initial_bars if initialized_now else 0}
        full_attempt = initialized_now
        state = self.coverage.setdefault(asset, {}).setdefault(timeframe, {})
        # Waiting callers recheck this inside the synchronizer lock. Reuse prices,
        # not a response: each caller still gets its own cutoff/window/finality.
        fetched_at = getattr(self, "_successful_refresh_at", {}).get((asset, timeframe))
        recent_fetch = (fetched_at is not None
                        and 0 <= time.monotonic() - fetched_at < REFRESH_REUSE_SECONDS)
        if not initialized_now and (stored.empty or (requested_at > stored.index.max() and not recent_fetch)):
            now = current_utc_time()
            estimate = self.settings.initial_bars if stored.empty else max(1,
                math.ceil((now - stored.index.max()) / TIMEFRAME_DURATIONS[timeframe])
                + self.settings.refresh_overlap_bars)
            request_bars = min(self.settings.initial_bars, estimate)
            full_attempt = request_bars >= min(self.settings.initial_bars, 5000)
            try:
                stored, received = self._fetch(asset, timeframe, requested_at, request_bars)
                meta.update(refreshed=True, reason="refresh_from_now", bars_requested=request_bars, bars_received=received)
                if not full_attempt and not self._last_fetch[(asset, timeframe)]["overlap"]:
                    stored, received = self._fetch(asset, timeframe, requested_at, self.settings.initial_bars)
                    full_attempt = True
                    meta.update(reason="full_batch_no_overlap", bars_requested=self.settings.initial_bars, bars_received=received)
            except Exception as exc:
                if stored.empty:
                    raise
                state["refresh_error"] = str(exc)
            self._audit(asset)

        view = self._served_view(asset, timeframe, stored, requested_at)
        cutoff = min(requested_at, current_utc_time())
        opened = view.loc[view.index <= cutoff]
        start = required_start
        if start is None and not opened.empty:
            if sessions is not None:
                from .windows import select_sessions
                start = select_sessions(opened, sessions).index.min()
            else:
                start = opened.tail(count).index.min() if count else opened.index.min()
        if (state.get("audit_from") and start is not None
                and pd.Timestamp(start) < pd.Timestamp(state["audit_from"])):
            # A bounded startup check must not imply coverage of older queries.
            self._audit(asset)
            view.attrs["history_coverage"] = deepcopy(state)
        coverage = window_coverage(view, cutoff, start, count=count, sessions=sessions)
        keys = [self._gap_key(g) for g in coverage["unresolved_gaps"]
                if not g.get("terminal") or g.get("repair_needed")]
        if coverage.get("outside_available_history"):
            # All requests older than the available provider/cache range share a
            # repair marker, not one key for every historical timestamp.
            keys.append("before_available_history")
        attempted = set(state.get("attempted_gaps", []))
        if keys and not full_attempt and set(keys) - attempted:
            try:
                stored, received = self._fetch(asset, timeframe, requested_at, self.settings.initial_bars)
                meta.update(refreshed=True, reason="full_batch_coverage", bars_requested=self.settings.initial_bars, bars_received=received)
            except Exception as exc:
                state["refresh_error"] = str(exc)
            full_attempt = True
            self._audit(asset)
            # A repair may change prices, receipts or calendar suspensions.
            # Ordinary cache hits keep the already normalized price view.
            view = None
        if full_attempt:
            state["attempted_gaps"] = sorted(attempted | set(keys) | {
                self._gap_key(g) for g in state.get("unresolved_gaps", [])})
        self._save_coverage(asset)
        if view is None:
            view = self._served_view(asset, timeframe, stored, requested_at)
        else:
            view.attrs["history_coverage"] = deepcopy(state)
        meta["history_coverage"] = window_coverage(view, cutoff, start, count=count, sessions=sessions)
        return view, meta

    def _fetch(self, asset, timeframe, requested_at, request_bars):
        if not hasattr(self, "_successful_refresh_at"):
            self._successful_refresh_at = {}
        self._successful_refresh_at.pop((asset, timeframe), None)
        try:
            old = self._read_prices(asset, timeframe)
            fresh = self._download_with_retries(asset, timeframe, request_bars)
            fetch = {"asset": asset, "timeframe": timeframe, "requested_at": requested_at.isoformat(),
                     "request_started_at": fresh.attrs["request_started_at"].isoformat(),
                     "bars_requested": request_bars, "attempt_bars": fresh.attrs["attempt_bars"],
                     "bars_received": len(fresh), "first": fresh.index.min().isoformat(),
                     "last": fresh.index.max().isoformat(),
                     "overlap": old.empty or bool(len(old.index.intersection(fresh.index))),
                     "bars_added": len(fresh.index.difference(old.index))}
            merged = self.storage.merge_and_write(
                asset, fresh, timeframe, received_after=fresh.attrs["request_started_at"])
        except Exception:
            self.control_log.record(asset, timeframe, requested_at, request_bars, "failure")
            raise
        self.control_log.record(asset, timeframe, requested_at, request_bars, "success")
        self._last_fetch[(asset, timeframe)] = fetch
        state = self.coverage.setdefault(asset, {}).setdefault(timeframe, {})
        state.pop("refresh_error", None)
        state["last_download"] = fetch
        with (self.settings.data_root / "download_coverage.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(fetch) + "\n")
        if timeframe == "1h" and suspend_conflicts(self.calendars.get(asset), self._frames(asset), fresh):
            self._save_calendar(asset)
        self._successful_refresh_at[(asset, timeframe)] = time.monotonic()
        return merged, len(fresh)

    def _served_view(self, asset, timeframe, source, requested_at):
        prices = source.copy(deep=False)
        prices.attrs = {}
        view = self._normalize_view(asset, timeframe, prices, requested_at)
        # Preserve raw identity/evidence through normalized daily and weekly labels.
        view = view.copy()
        view.attrs["completion_context"] = {
            "timeframe": timeframe, "calendar": deepcopy(self.calendars.get(asset)),
            "raw_opens": [t.isoformat() for t in source.index],
            "receipts": source.attrs.get("receipts", {}),
        }
        view.attrs["history_coverage"] = deepcopy(self.coverage.get(asset, {}).get(timeframe, {}))
        return view

    def _normalize_view(self, asset, timeframe, source, requested_at):
        if (self.settings.market_rules is not None or self.settings.timestamp_profiles is not None) and timeframe in {"1D", "1W"}:
            from .provider import split_asset
            from .timestamp_profiles import served_profile
            symbol, exchange = split_asset(asset)
            rule = resolve_market_rule(self.settings, asset)
            profile = (rule.timestamp_profile if self.settings.market_rules is not None else
                       self.settings.timestamp_profiles.get(f"{exchange}:{symbol}"))
            if profile is None:
                return source
            return served_profile(
                source, source if timeframe == "1D" else self._read_prices(asset, "1D"),
                self._read_prices(asset, "4h"), self._read_prices(asset, "1h"),
                min(requested_at, pd.Timestamp.now(tz="UTC")), timeframe, profile, rule.timezone,
            )
        if not uses_trading_dates(asset, timeframe, self.settings.rus_daily_trading_date_assets):
            return source
        if timeframe == "1W":
            return normalize_weekly(
                source,
                self._read_prices(asset, "1D"),
                self._read_prices(asset, "4h"),
                min(requested_at, pd.Timestamp.now(tz="UTC")),
                hourly=self._read_prices(asset, "1h"),
            )
        return normalize_daily(
            source,
            self._read_prices(asset, "4h"),
            min(requested_at, pd.Timestamp.now(tz="UTC")),
            hourly=self._read_prices(asset, "1h"),
        )

    def _download_with_retries(
        self, asset: str, timeframe: str, request_bars: int
    ) -> pd.DataFrame:
        attempt_sizes = (
            min(request_bars, 5000),
            min(request_bars, 5000),
            min(request_bars, 5000),
            min(request_bars, 4000),
            min(request_bars, 2000),
        )
        for attempt, bars in enumerate(attempt_sizes, start=1):
            try:
                started = pd.Timestamp.now(tz="UTC")
                fresh = self.provider.get_history(asset, timeframe, bars)
                if fresh is None or fresh.empty:
                    raise RuntimeError(f"provider returned no data for {asset}")
                fresh.attrs["request_started_at"] = started
                fresh.attrs["attempt_bars"] = bars
                return fresh
            except Exception:
                if attempt == len(attempt_sizes):
                    raise
                time.sleep(5)
        raise RuntimeError("unreachable")

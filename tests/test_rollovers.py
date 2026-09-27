from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from tv_history import provider_rollovers as adapter
from tv_history.rollovers import RolloverCalendars, merge_snapshot
from tv_history.storage import CsvStorage, atomic_json
from tv_history.sync import HistorySynchronizer
from test_sync import make_settings, frame


PLOTS = [{"id": n + suffix} for suffix in ("", "2", "3", "4")
         for n in ("CurrentContractCode", "NextContractCode", "SwitchDate")] + [{"id": "Overflowed"}]
SPEC = {"id": adapter.STUDY + "-123", "plots": PLOTS}
NOW = pd.Timestamp("2026-09-24T08:00Z")
ASSET = "ICEEUR:BRN1!"


def event_row(index=-100, stamp=1792965600, old=202612, new=202701, date=20261026):
    return {"i": index, "v": [stamp, old, new, date] + [1e100] * 9 + [0]}


def snapshot():
    return {"source": SPEC["id"], "timezone": "Europe/London",
            "events": adapter.decode_events([event_row()], PLOTS, "ICEEUR", "BRN")}


def wire(value):
    payload = value if isinstance(value, str) else json.dumps(value)
    return f"~m~{len(payload)}~m~{payload}"


class Socket:
    def __init__(self, failure=None, empty=False):
        self.sent, self.chunks = [], []
        self.closed = False
        self.failure, self.empty = failure, empty

    def send(self, payload):
        if "~h~" in payload:
            self.heartbeat = payload
            return
        msg = json.loads(payload.split("~m~", 2)[2])
        self.sent.append(msg)
        method, params = msg["m"], msg["p"]
        if method == "chart_create_session":
            self.session = params[0]
        if method == "request_studies_metadata":
            data = wire({"m": "studies_metadata", "p": ["", {"metainfo": [SPEC]}]})
            data += wire("~h~42")
            data += wire({"m": "symbol_resolved", "p": [self.session, "symbol_1", {
                "type": "futures", "continuous_order": 1, "root": "BRN", "timezone": "Europe/London"}]})
            self.chunks.extend([data[:1], data[1:11], data[11:]])
        if method == "create_study":
            study = params[1]
            if self.failure:
                self.chunks.append(wire({"m": self.failure, "p": [self.session, study, "private-token"]}))
            else:
                rows = [] if self.empty else [event_row(), event_row(500, 1790208000, 202611, 202612, 20260924)]
                self.chunks.append(wire({"m": "timescale_update", "p": [self.session, {study: {"st": rows}}]}))
                self.chunks.append(wire({"m": "study_completed", "p": [self.session, study, "s1_st1"]}))

    def recv(self):
        if not self.chunks:
            raise TimeoutError("private-token")
        return self.chunks.pop(0)

    def settimeout(self, timeout):
        assert timeout > 0

    def close(self):
        self.closed = True


def test_wire_discovery_fragments_heartbeat_and_outside_window_events(monkeypatch):
    ws = Socket()
    monkeypatch.setattr(adapter, "create_connection", lambda *a, **k: ws)
    result = adapter.collect_rollovers(ASSET, "private-token")
    assert ws.closed and ws.heartbeat == wire("~h~42")
    assert len(result["events"]) == 2
    assert result["source"] == SPEC["id"]  # discovered version, not pinned to 48
    upcoming = result["events"][-1]
    assert upcoming["to"] == "ICEEUR:BRNF2027"
    assert upcoming["trading_date"] == "2026-10-26"
    assert upcoming["scheduled_at_utc"] == "2026-10-25T22:00:00+00:00"
    assert pd.Timestamp(upcoming["scheduled_at_utc"]).tz_convert(result["timezone"]).isoformat() == upcoming["scheduled_at_utc"]
    assert next(m for m in ws.sent if m["m"] == "create_study")["p"][4] == SPEC["id"]


@pytest.mark.parametrize("failure", ["study_error", "protocol_error", "series_error"])
def test_source_errors_never_return_partial_success_or_secrets(monkeypatch, failure):
    ws = Socket(failure)
    monkeypatch.setattr(adapter, "create_connection", lambda *a, **k: ws)
    with pytest.raises(adapter.RolloverError) as exc:
        adapter.collect_rollovers(ASSET, "private-token")
    assert "private-token" not in str(exc.value)
    assert ws.closed


def test_completed_empty_is_distinguished_from_failure(monkeypatch):
    ws = Socket(empty=True)
    monkeypatch.setattr(adapter, "create_connection", lambda *a, **k: ws)
    assert adapter.collect_rollovers(ASSET, None)["events"] == []


@pytest.mark.parametrize("index,value", [(2, 202713), (4, 202701), (13, 1)])
def test_invalid_contract_partial_group_or_overflow_rejected(index, value):
    row = event_row()
    row["v"][index] = value
    with pytest.raises(adapter.RolloverError):
        adapter.decode_events([row], PLOTS, "ICEEUR", "BRN")


def test_merge_updates_same_pair_and_preserves_missing_history():
    initial = merge_snapshot({}, snapshot(), NOW)
    revised = snapshot()
    revised["events"][0]["scheduled_at_utc"] = "2026-10-25T23:00:00+00:00"
    result = merge_snapshot(initial, revised, NOW + pd.Timedelta(days=1))
    assert len(result["events"]) == 1
    assert result["events"][0]["first_seen_at_utc"] == NOW.isoformat()
    assert result["events"][0]["last_seen_at_utc"] != NOW.isoformat()
    empty = merge_snapshot(result, {**revised, "events": []}, NOW + pd.Timedelta(days=2))
    assert empty["events"] == result["events"]
    assert empty["last_result"] == "empty"
    assert initial["events"][0]["scheduled_at_utc"] != result["events"][0]["scheduled_at_utc"]


def test_startup_once_restart_aliases_failure_and_unchanged_prices(tmp_path):
    store = CsvStorage(make_settings(tmp_path))
    prices = frame("2026-09-01", [100., 101.])
    store.merge_and_write(ASSET, prices)
    path = store.path_for(ASSET)
    before = path.read_bytes()
    calls = []
    def getter(asset):
        calls.append(asset)
        return snapshot()
    provider = SimpleNamespace(get_rollovers=getter)
    service = RolloverCalendars(provider, store)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda a: service.refresh(a, NOW), [ASSET, "BRN1!:ICEEUR"] * 2))
    assert calls == [ASSET]
    restarted = RolloverCalendars(provider, store)
    restarted.refresh(ASSET, NOW + pd.Timedelta(minutes=1))
    assert len(calls) == 2  # a fresh saved calendar never suppresses startup
    restarted.refresh(ASSET, NOW + pd.Timedelta(days=1))
    assert len(calls) == 2
    near = pd.Timestamp("2026-10-25T20:00Z")
    restarted.refresh(ASSET, near)
    restarted.refresh(ASSET, near + pd.Timedelta(minutes=59))
    assert len(calls) == 2
    restarted.refresh(ASSET, near + pd.Timedelta(hours=1))
    assert len(calls) == 2
    saved = deepcopy(store.read_rollovers(ASSET))
    def failed(asset):
        calls.append(asset)
        raise RuntimeError("private-token")
    provider.get_rollovers = failed
    restarted = RolloverCalendars(provider, store)
    restarted.refresh(ASSET, near + pd.Timedelta(hours=2))
    error = store.read_rollovers(ASSET)
    assert error["events"] == saved["events"]
    assert error["checked_at_utc"] == saved["checked_at_utc"]
    assert error["last_error"] == "source_unavailable"
    restarted.refresh(ASSET, near + pd.Timedelta(hours=2, minutes=59))
    restarted.refresh(ASSET, near + pd.Timedelta(days=10))
    assert len(calls) == 3  # failures also wait until another startup
    assert path.read_bytes() == before
    assert "private-token" not in (path.parent / "rollovers.json").read_text()


def test_only_startup_collects_metadata_not_historical_requests(tmp_path, monkeypatch, capsys):
    from test_history_coverage import seeded, Provider, ASSET as NQ
    prices = frame("2026-09-01", [100., 101., 102.])
    provider = Provider([])
    calls = []
    provider.get_rollovers = lambda asset: (calls.append(asset) or snapshot())
    monkeypatch.setattr("tv_history.sync.current_utc_time", lambda: NOW)
    _, store, sync = seeded(tmp_path, prices, provider)
    sync.ensure_available(NQ, "1h", pd.Timestamp("2026-09-01T01:00Z"), count=1)
    assert calls == []
    assert provider.calls == []
    assert store.read_rollovers(NQ) == {}
    assert capsys.readouterr().err == ""
    cfg = replace(make_settings(tmp_path / "startup"), rus_daily_trading_date_assets=())
    atomic_json(cfg.data_root / "ticker_registry.json", {ASSET: []})
    HistorySynchronizer(cfg, provider, CsvStorage(cfg), refresh_on_start=True)
    assert calls == [ASSET]
    progress = capsys.readouterr()
    assert progress.out == ""  # stdout is reserved for MCP protocol messages
    assert "rollover calendars" in progress.err
    assert "initialization complete" in progress.err
    assert ASSET not in progress.err
    # This metadata-only fixture has no price cache to retain in the registry.
    atomic_json(cfg.data_root / "ticker_registry.json", {ASSET: []})
    HistorySynchronizer(cfg, provider, CsvStorage(cfg), refresh_on_start=True)
    assert calls == [ASSET, ASSET]
    monkeypatch.setattr("tv_history.sync.current_utc_time", lambda: NOW + pd.Timedelta(days=40))
    sync.ensure_available(NQ, "1h", pd.Timestamp("2026-09-01T01:00Z"), count=1)
    assert calls == [ASSET, ASSET]


def test_other_assets_are_not_queried(tmp_path):
    provider = SimpleNamespace(get_rollovers=lambda a: pytest.fail("unexpected lookup"))
    store = CsvStorage(make_settings(tmp_path))
    service = RolloverCalendars(provider, store)
    for asset in ["RUS:SBER", "BITSTAMP:BTCUSD", "ICEEUR:BRNZ2026", "ICEEUR:BRN2!"]:
        service.refresh(asset, NOW)
        assert not (store.path_for(asset).parent / "rollovers.json").exists()


def test_future_horizon_uses_daily_dates_but_preserves_hourly_precision():
    hourly = snapshot()["events"]
    daily = deepcopy(hourly)
    daily[0]["scheduled_at_utc"] = "2026-10-25T00:00:00+00:00"
    for month in (11, 12):
        daily.append({"from": f"ICEEUR:BRN{month}", "to": f"ICEEUR:BRN{month + 1}",
                      "trading_date": f"2026-{month}-24", "scheduled_at_utc": f"2026-{month}-24T01:00:00+00:00"})
    daily.insert(0, {"from": "old", "to": "older", "trading_date": "2025-01-01",
                     "scheduled_at_utc": "2025-01-01T00:00:00+00:00"})
    events = adapter.combine_calendars(hourly, daily, NOW.to_pydatetime())
    assert len(events) == 3
    assert events[0]["scheduled_at_utc"] == hourly[0]["scheduled_at_utc"]
    assert [e["timestamp_basis"] for e in events] == ["1h", "1D", "1D"]
    document = merge_snapshot({}, {**snapshot(), "events": events}, NOW)
    assert document["future_available"] == 3 and document["future_status"] == "ready"
    assert merge_snapshot({}, snapshot(), NOW)["future_status"] == "limited"


def test_local_timestamps_include_dst_and_upgrade_without_prices(tmp_path):
    store = CsvStorage(make_settings(tmp_path))
    old = {"schema_version": 1, "timezone": "Europe/London", "last_attempt_at_utc": NOW.isoformat(),
           "checked_at_utc": NOW.isoformat(), "events": [
               {"from": "ICEEUR:BRNX2026", "to": "ICEEUR:BRNZ2026", "trading_date": "2026-09-24",
                "scheduled_at_utc": "2026-09-24T00:00:00+00:00"}, *snapshot()["events"]]}
    store.save_rollovers(ASSET, old)
    def failed(asset):
        raise RuntimeError("offline")
    service = RolloverCalendars(SimpleNamespace(get_rollovers=failed), store)
    service.refresh(ASSET, NOW)
    upgraded = store.read_rollovers(ASSET)
    assert upgraded["schema_version"] == 2
    assert upgraded["checked_at_utc"] == old["checked_at_utc"]
    assert upgraded["events"][0]["scheduled_at_local"] == "2026-09-24T01:00:00+01:00"
    assert upgraded["events"][1]["scheduled_at_local"] == "2026-10-25T22:00:00+00:00"
    assert upgraded["events"][0]["scheduled_at_utc"] == old["events"][0]["scheduled_at_utc"]
    assert all(e["timestamp_basis"] == "1h" for e in upgraded["events"])
    assert not store.path_for(ASSET).exists()


def test_hourly_event_replaces_daily_anchor_for_same_pair():
    daily = snapshot()
    daily["events"][0].update(timestamp_basis="1D", scheduled_at_utc="2026-10-25T00:00:00+00:00")
    old = merge_snapshot({}, daily, NOW)
    hourly = snapshot()
    hourly["events"][0]["timestamp_basis"] = "1h"
    result = merge_snapshot(old, hourly, NOW + pd.Timedelta(days=1))
    assert len(result["events"]) == 1
    assert result["events"][0]["timestamp_basis"] == "1h"
    assert result["events"][0]["scheduled_at_local"] == "2026-10-25T22:00:00+00:00"
    assert result["events"][0]["first_seen_at_utc"] == NOW.isoformat()

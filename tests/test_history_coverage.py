from dataclasses import replace
from zipfile import ZipFile

import pandas as pd
import pytest

from tv_history.sync import HistorySynchronizer
from tv_history.storage import CsvStorage, atomic_json
from tv_history.coverage import audit_gaps, scheduled_gap, window_coverage
from tv_history.analysis import AssetAnalysisService
from test_sync import make_settings, frame


ASSET = "CME_MINI:NQ1!"


class Provider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get_history(self, asset, timeframe, n_bars):
        self.calls.append((asset, timeframe, n_bars))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value.tail(n_bars).copy()


def seeded(tmp_path, hourly, provider, others=None, **kwargs):
    cfg = replace(make_settings(tmp_path), refresh_overlap_bars=5,
                  rus_daily_trading_date_assets=(), market_rules={})
    storage = CsvStorage(cfg)
    for tf in ("1h", "4h", "1D", "1W"):
        storage.merge_and_write(ASSET, hourly if tf == "1h" else (others if others is not None else hourly.tail(1)), tf)
    atomic_json(cfg.data_root / "ticker_registry.json", {ASSET: ["1h", "4h", "1D", "1W"]})
    return cfg, storage, HistorySynchronizer(cfg, provider, storage, **kwargs)


def test_historical_nq_request_is_sized_from_download_time(tmp_path, monkeypatch):
    monkeypatch.setattr("tv_history.sync.current_utc_time", lambda: pd.Timestamp("2026-09-06T06:54Z"))
    old = frame("2026-08-18T06:00Z", [100.] * 25)
    fresh = frame("2026-08-18T00:00Z", [100.] * 429)
    provider = Provider([fresh])
    _, _, sync = seeded(tmp_path, old, provider)
    view, meta = sync.ensure_available(ASSET, "1h", pd.Timestamp("2026-08-24T00:00Z"))
    assert provider.calls == [(ASSET, "1h", 438)]
    assert pd.Timestamp("2026-08-24T00:00Z") in view.index
    assert meta["history_coverage"]["status"] == "no_known_gaps"


def test_disjoint_small_response_triggers_one_full_batch(tmp_path, monkeypatch):
    monkeypatch.setattr("tv_history.sync.current_utc_time", lambda: pd.Timestamp("2026-09-06T06:54Z"))
    old = frame("2026-08-18T06:00Z", [100.] * 25)
    small = frame("2026-08-28T17:00Z", [100.] * 120)
    full = frame("2026-08-18T00:00Z", [100.] * 450)
    provider = Provider([small, full])
    _, _, sync = seeded(tmp_path, old, provider)
    view, meta = sync.ensure_available(ASSET, "1h", pd.Timestamp("2026-08-24T00:00Z"))
    assert [c[2] for c in provider.calls] == [438, 5000]
    assert meta["reason"] == "full_batch_no_overlap"
    assert pd.Timestamp("2026-08-24T00:00Z") in view.index


def test_unresolved_interior_gap_is_not_covered_or_retried_forever(tmp_path):
    old = frame("2026-08-19T06:00Z", [100.])
    latest = frame("2026-08-28T17:00Z", [100.] * 3)
    provider = Provider([latest])
    hourly = pd.concat([old, latest])
    _, storage, sync = seeded(tmp_path, hourly, provider)
    target = pd.Timestamp("2026-08-24T00:00Z")
    for _ in range(2):
        _, meta = sync.ensure_available(ASSET, "1h", target)
        assert meta["history_coverage"]["status"] == "uncertain"
        assert len(meta["history_coverage"]["unresolved_gaps"]) == 1
    assert len(provider.calls) == 1
    restarted_provider = Provider([])
    restarted = HistorySynchronizer(sync.settings, restarted_provider, storage)
    restarted.ensure_available(ASSET, "1h", target)
    assert restarted_provider.calls == []


def test_pre_cache_cutoffs_share_one_failed_repair_marker(tmp_path):
    latest = frame("2026-08-28T17:00Z", [100.] * 3)
    provider = Provider([latest])
    _, _, sync = seeded(tmp_path, latest, provider)
    for date in ("2026-08-24", "2026-08-23"):
        _, meta = sync.ensure_available(ASSET, "1h", pd.Timestamp(date, tz="UTC"))
        assert meta["history_coverage"]["status"] == "incomplete"
        assert meta["history_coverage"]["outside_available_history"]
    assert len(provider.calls) == 1


def test_startup_backup_precedes_only_affected_timeframe_repair(tmp_path):
    fresh = frame("2026-08-19T06:00Z", [100.] * 48)
    old = fresh.iloc[[0, -1]]
    provider = Provider([fresh])
    cfg, storage, sync = seeded(tmp_path, old, provider, refresh_on_start=True)
    assert provider.calls == [(ASSET, "1h", 5000)]
    assert len(storage.read(ASSET, "1h")) == 48
    with ZipFile(sync.backup_path) as archive:
        original = archive.read(str(storage.path_for(ASSET).relative_to(cfg.data_root)).replace("\\", "/"))
        assert len(original.decode().splitlines()) == 3
    again = Provider([])
    HistorySynchronizer(cfg, again, storage, refresh_on_start=True)
    assert again.calls == []


def test_startup_failure_preserves_cache_and_continues_other_timeframes(tmp_path, monkeypatch):
    monkeypatch.setattr("tv_history.sync.time.sleep", lambda _: None)
    old = frame("2026-08-19T06:00Z", [100.] * 49).iloc[[0, -1]]
    other = frame("2026-08-19T06:00Z", [100.] * 13, "4h")
    provider = Provider([RuntimeError("offline")] * 5 + [other, other])
    cfg, storage, sync = seeded(tmp_path, old, provider, others=old, refresh_on_start=True)
    assert len(storage.read(ASSET, "1h")) == 2
    assert sync.coverage[ASSET]["1h"]["refresh_error"] == "offline"
    assert [c[1] for c in provider.calls] == ["1h"] * 5 + ["4h", "1D"]
    again = Provider([])
    restarted = HistorySynchronizer(cfg, again, storage, refresh_on_start=True)
    assert again.calls == []
    assert restarted.coverage[ASSET]["1h"]["refresh_error"] == "offline"
    assert restarted.coverage[ASSET]["1h"]["unresolved_gaps"]


def test_unchanged_startup_reads_no_prices_and_reuses_calendar(tmp_path, monkeypatch):
    cfg, storage, sync = seeded(tmp_path, frame("2025-01-01", [100.] * 24),
                                Provider([]), refresh_on_start=True)
    saved = sync.coverage[ASSET]["startup_check"]
    version = sync.calendars[ASSET]["version"]
    def unexpected(*args, **kwargs):
        pytest.fail("unchanged startup must not read prices, learn or audit")
    monkeypatch.setattr(storage, "read", unexpected)
    monkeypatch.setattr("tv_history.sync.learn_calendar", unexpected)
    monkeypatch.setattr("tv_history.sync.audit_gaps", unexpected)
    provider = Provider([])
    restarted = HistorySynchronizer(cfg, provider, storage, refresh_on_start=True)
    assert provider.calls == []
    assert restarted.calendars[ASSET]["version"] == version
    assert restarted.coverage[ASSET]["startup_check"] == saved


@pytest.mark.parametrize("change", ["prices", "settings", "checker", "calendar_missing", "metadata_missing", "file_missing"])
def test_startup_metadata_invalidates_on_changed_inputs(tmp_path, monkeypatch, change):
    cfg, storage, sync = seeded(tmp_path, frame("2026-08-01", [100.] * 24),
                                Provider([]), refresh_on_start=True)
    before = sync.calendars[ASSET]["version"]
    if change == "prices":
        storage.merge_and_write(ASSET, frame("2026-08-01", [99.]), "1h")
    elif change == "settings":
        cfg = replace(cfg, calendar_sessions=31)
    elif change == "checker":
        monkeypatch.setattr("tv_history.sync.STARTUP_CHECK_VERSION", 2)
    elif change == "calendar_missing":
        (storage.path_for(ASSET).parent / "calendar.json").unlink()
    elif change == "metadata_missing":
        sync.coverage[ASSET].pop("startup_check")
        sync._save_coverage(ASSET)
    elif change == "file_missing":
        storage.path_for(ASSET, "4h").unlink()
    provider = Provider([])
    restarted = HistorySynchronizer(cfg, provider, storage, refresh_on_start=True)
    assert restarted.calendars[ASSET]["version"] != before
    assert provider.calls == []
    if change == "file_missing":
        assert "4h" not in restarted._initialized[ASSET]


def test_startup_rechecks_new_gap_after_data_changes(tmp_path):
    full = frame("2026-08-01", [100.] * 24)
    cfg, storage, _ = seeded(tmp_path, full, Provider([]), refresh_on_start=True)
    storage._atomic_write(storage.path_for(ASSET), full.drop(full.index[5]))
    provider = Provider([full])
    restarted = HistorySynchronizer(cfg, provider, storage, refresh_on_start=True)
    assert provider.calls == [(ASSET, "1h", 5000)]
    assert restarted.coverage[ASSET]["1h"]["unresolved_gaps"] == []


def test_startup_does_not_retry_unresolved_gap_after_unrelated_write(tmp_path):
    hole = frame("2026-08-01", [100.] * 24).iloc[[0, -1]]
    cfg, storage, _ = seeded(tmp_path, hole, Provider([hole]), refresh_on_start=True)
    storage.merge_and_write(ASSET, hole.tail(1), "4h")
    provider = Provider([])
    restarted = HistorySynchronizer(cfg, provider, storage, refresh_on_start=True)
    assert provider.calls == []
    assert restarted.coverage[ASSET]["1h"]["unresolved_gaps"]


def test_startup_window_does_not_hide_older_gaps_from_requests(tmp_path):
    old = frame("2026-01-01", [100.] * 12).iloc[[0, -1]]
    recent = frame("2026-01-02", [100.] * (24 * 200))
    hourly = pd.concat([old, recent])
    provider = Provider([])
    cfg, storage, sync = seeded(tmp_path, hourly, provider, refresh_on_start=True)
    assert provider.calls == []
    assert sync.coverage[ASSET]["1h"]["unresolved_gaps"] == []
    # Outside the startup window, a normal historical query still audits and repairs.
    provider.responses.append(hourly)
    _, meta = sync.ensure_available(ASSET, "1h", pd.Timestamp("2026-01-01T08:00Z"))
    assert provider.calls == [(ASSET, "1h", 5000)]
    assert meta["history_coverage"]["unresolved_gaps"]


def test_startup_catches_gap_crossing_100_day_boundary(tmp_path):
    hourly = pd.concat([frame("2026-01-01", [100.]), frame("2026-05-01", [100.] * 24)])
    provider = Provider([hourly])
    _, _, sync = seeded(tmp_path, hourly, provider, refresh_on_start=True)
    assert provider.calls == [(ASSET, "1h", 5000)]
    assert sync.coverage[ASSET]["1h"]["unresolved_gaps"]


def test_startup_repairs_tail_only_with_other_cached_trading_evidence(tmp_path):
    full = frame("2026-08-01", [100.] * 13)
    provider = Provider([full])
    _, _, sync = seeded(tmp_path, full.iloc[:2], provider,
                        others=full.iloc[::4], refresh_on_start=True)
    assert provider.calls == [(ASSET, "1h", 5000)]
    assert sync.coverage[ASSET]["1h"]["unresolved_gaps"] == []


def test_cached_view_is_normalized_once_and_keeps_updated_coverage(tmp_path, monkeypatch):
    full = frame("2026-08-01", [100.] * 24)
    _, _, sync = seeded(tmp_path, full, Provider([]))
    calls = []
    original = sync._normalize_view
    def tracked(*args):
        calls.append(args[1])
        return original(*args)
    monkeypatch.setattr(sync, "_normalize_view", tracked)
    source, meta = sync.ensure_available(ASSET, "1h", full.index[-1], count=5)
    assert calls == ["1h"]
    assert meta["history_coverage"]["status"] == "no_known_gaps"
    assert source.attrs["history_coverage"] == sync.coverage[ASSET]["1h"]


def test_coverage_repair_rebuilds_view_with_new_prices_and_receipts(tmp_path):
    fresh = frame("2026-08-01", [100., 101., 102.])
    old = fresh.iloc[[0, -1]]
    _, _, sync = seeded(tmp_path, old, Provider([fresh]))
    source, meta = sync.ensure_available(ASSET, "1h", fresh.index[-1], count=3)
    assert source.loc[fresh.index[1], "close"] == 101.
    assert meta["history_coverage"]["status"] == "no_known_gaps"
    # The repaired older bar has a successor; its receipt is no longer needed.
    assert fresh.index[1].isoformat() not in source.attrs["completion_context"]["receipts"]
    assert source.attrs["completion_context"]["receipts"][fresh.index[-1].isoformat()]["ohlcv"][3] == 102.
    assert source.attrs["history_coverage"] == sync.coverage[ASSET]["1h"]


@pytest.mark.parametrize("include_coverage_metadata", [False, True])
def test_coverage_diagnostics_do_not_change_repairs(tmp_path, include_coverage_metadata):
    fresh = frame("2026-08-19T06:00Z", [100.] * 48)
    provider = Provider([fresh])
    _, storage, sync = seeded(tmp_path, fresh.iloc[[0, -1]], provider)
    for _ in range(2):
        view, meta = sync.ensure_available(
            ASSET, "1h", fresh.index[-1],
            include_coverage_metadata=include_coverage_metadata,
        )
        assert ("history_coverage" in meta) is include_coverage_metadata
        pd.testing.assert_frame_equal(view[list(fresh.columns)], fresh, check_freq=False)
        assert window_coverage(view, fresh.index[-1])["status"] == "no_known_gaps"
    assert provider.calls == [(ASSET, "1h", 5000)]
    assert len(storage.read(ASSET, "1h")) == len(fresh)


def test_skipping_coverage_metadata_avoids_extra_window_check(tmp_path, monkeypatch):
    import tv_history.sync as sync_module
    prices = frame("2026-08-19T06:00Z", [100.] * 48)
    _, _, sync = seeded(tmp_path, prices, Provider([]))
    calls = []
    original = sync_module.window_coverage

    def tracked(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(sync_module, "window_coverage", tracked)
    sync.ensure_available(ASSET, "1h", prices.index[-1], include_coverage_metadata=False)
    assert len(calls) == 1  # Repair eligibility still runs.
    calls.clear()
    sync.ensure_available(ASSET, "1h", prices.index[-1], include_coverage_metadata=True)
    assert len(calls) == 2


def test_cross_timeframe_evidence_confirms_gap_and_blocks_analysis(tmp_path):
    hourly = pd.concat([frame("2026-08-19T06:00Z", [100.]), frame("2026-08-28T17:00Z", [100.])])
    provider = Provider([hourly])
    cfg, storage, sync = seeded(tmp_path, hourly, provider)
    storage.merge_and_write(ASSET, frame("2026-08-24T00:00Z", [100.]), "4h")
    sync._audit(ASSET)
    assert sync.coverage[ASSET]["1h"]["unresolved_gaps"][0]["status"] == "incomplete"
    response = AssetAnalysisService(cfg, sync).analyze(ASSET, "1h", "2026-08-24T12:00Z")
    assert response["error"]["code"] == "INSUFFICIENT_HISTORY_COVERAGE"


def test_only_exact_verified_resumption_is_scheduled():
    cal = {"status": "ready", "timezone": "UTC", "rules": {
        "1h|4:20:00": {"next_offset": [3, 0, 0]}}}
    before = pd.Timestamp("2026-07-17T20:00Z")
    assert scheduled_gap(cal, "1h", before, pd.Timestamp("2026-07-20T00:00Z"))
    assert not scheduled_gap(cal, "1h", before, pd.Timestamp("2026-07-27T00:00Z"))


def test_stale_tail_is_not_silently_covered(tmp_path):
    old = frame("2026-08-19T06:00Z", [100.])
    _, _, sync = seeded(tmp_path, old, Provider([]))
    source = sync._served_view(ASSET, "1h", old, pd.Timestamp("2026-08-24T12:00Z"))
    quality = window_coverage(source, pd.Timestamp("2026-08-24T12:00Z"))
    assert quality["status"] == "uncertain"
    assert quality["unresolved_gaps"][0]["terminal"]


def test_short_requested_window_reports_insufficient_history(tmp_path):
    latest = frame("2026-08-28T17:00Z", [100.] * 3)
    provider = Provider([latest])
    _, _, sync = seeded(tmp_path, latest, provider)
    _, meta = sync.ensure_available(ASSET, "1h", latest.index[-1], count=100)
    assert meta["history_coverage"]["status"] == "incomplete"
    assert len(provider.calls) == 1


def test_daily_holiday_tail_is_not_confirmed_missing_from_activity_alone():
    daily = frame("2026-09-04T22:00Z", [100., 100.], "4D")
    frames = {"1h": frame("2026-09-07T22:00Z", [100.] * 12),
              "4h": frame("2026-09-07T22:00Z", [100.] * 3, "4h"),
              "1D": daily, "1W": daily.iloc[:0]}
    gaps = audit_gaps(ASSET, "1D", frames, None)
    assert gaps[0]["status"] == "uncertain"


def test_full_merge_does_not_copy_all_receipts_per_existing_row(tmp_path, monkeypatch):
    class Probe(int):
        copies = 0
        def __deepcopy__(self, memo):
            type(self).copies += 1
            return self
    storage = CsvStorage(make_settings(tmp_path))
    existing = frame("2026-08-01T00:00Z", [100.] * 200)
    fresh = existing.copy()
    existing.attrs["receipts"] = {"probe": Probe(1)}
    monkeypatch.setattr(storage, "read", lambda *args: existing)
    storage.merge_and_write(ASSET, fresh, "1h", pd.Timestamp("2026-09-09T00:00Z"))
    assert Probe.copies < 5


def test_daily_trading_label_does_not_prove_weekend_hourly_activity():
    hourly = pd.concat([frame("2026-07-10T20:00Z", [100.]), frame("2026-07-13T06:00Z", [100.])])
    frames = {"1h": hourly, "4h": frame("2026-07-13T05:00Z", [100.]),
              "1D": frame("2026-07-11T06:00Z", [100.]), "1W": hourly.iloc[:0]}
    assert audit_gaps("RUS:SI1!", "1h", frames, None)[0]["status"] == "uncertain"


def test_crypto_dst_shift_is_not_confirmed_missing_candle():
    daily = pd.concat([frame("2013-10-26T00:00Z", [100.]), frame("2013-10-27T01:00Z", [100.])])
    frames = {tf: daily.iloc[:0] for tf in ("1h", "4h", "1D", "1W")}
    frames["1D"] = daily
    assert audit_gaps("BITSTAMP:BTCUSD", "1D", frames, None)[0]["status"] == "uncertain"


def test_analysis_does_not_copy_receipts_in_price_row_loops(tmp_path):
    from test_analysis import hourly_frame, settings, FakeSynchronizer
    class Probe:
        copies = 0
        def __deepcopy__(self, memo):
            type(self).copies += 1
            return self
    prices = hourly_frame()
    prices.attrs["completion_context"] = {"receipts": {"probe": Probe()}}
    response = AssetAnalysisService(settings(tmp_path), FakeSynchronizer(prices)).analyze(
        "BITSTAMP:BTCUSD", "1h", "2026-01-11T09:30:00Z")
    assert "error" not in response
    assert Probe.copies < 100

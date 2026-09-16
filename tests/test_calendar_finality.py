from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pandas as pd
import pytest

from tv_history.calendar import learn_calendar, expected_boundary, suspend_conflicts, boundary_from_rule
from tv_history.finality import completion_details, finalization_delay
from tv_history.storage import CsvStorage, read_json
from tv_history.sync import HistorySynchronizer
from test_sync import make_settings, frame


ASSET = "NASDAQ:ABC"


def native_frames(weekends=False):
    dates = pd.date_range("2026-06-01", "2026-08-31", tz="UTC")
    stamps = {tf: [] for tf in ("1h", "4h", "1D", "1W")}
    for day in dates:
        if day.weekday() >= 5 and not weekends:
            continue
        hours = list(range(10, 13)) if day.weekday() >= 5 else list(range(9, 16))
        stamps["1h"].extend(day + pd.Timedelta(hours=h) for h in hours)
        stamps["4h"].extend(day + pd.Timedelta(hours=h) for h in hours[::4])
        stamps["1D"].append(day + pd.Timedelta(hours=hours[0]))
        if day.weekday() == 0:
            stamps["1W"].append(day + pd.Timedelta(hours=9))
    return {tf: pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100., "volume": 100.},
                            index=pd.DatetimeIndex(times, name="timestamp_utc"))
            for tf, times in stamps.items()}


def configured(tmp_path, **kwargs):
    return replace(make_settings(tmp_path), calendar_timezones={ASSET: "UTC"}, **kwargs)


def calendar(tmp_path, **kwargs):
    return learn_calendar(ASSET, native_frames(), configured(tmp_path, **kwargs), pd.Timestamp("2026-09-01T00:00Z"))


def with_evidence(cal, tf, stamp, fetched=None):
    raw = pd.Timestamp(stamp)
    f = frame(raw.isoformat(), [100.])
    receipts = {}
    if fetched:
        receipts[raw.isoformat()] = {"request_started_at": fetched, "received_at": fetched,
                                     "ohlcv": [100., 101., 99., 100., 100.]}
    f.attrs["completion_context"] = {"timeframe": tf, "calendar": cal,
                                      "raw_opens": [raw.isoformat()], "receipts": receipts}
    return f


def evaluate(f, tf, now, delay=5):
    durations = {"1h": "1h", "4h": "4h", "1D": "1D", "1W": "7D"}
    return completion_details(f, pd.Timedelta(durations[tf]), pd.Timestamp(now), pd.Timedelta(minutes=delay)).iloc[-1]


def test_calendar_requires_30_sessions_and_uses_actual_native_period_end(tmp_path):
    cal = calendar(tmp_path)
    assert cal["status"] == "ready"
    assert cal["eligible_sessions"] == 30
    for tf, opened, expected in [
        ("1h", "2026-09-01T15:00Z", "2026-09-01T16:00Z"),
        ("4h", "2026-09-01T13:00Z", "2026-09-01T16:00Z"),
        ("1D", "2026-09-01T09:00Z", "2026-09-01T16:00Z"),
        ("1W", "2026-09-07T09:00Z", "2026-09-11T16:00Z"),
    ]:
        assert expected_boundary(cal, tf, pd.Timestamp(opened), pd.Timestamp(expected)) == pd.Timestamp(expected)
    assert expected_boundary(cal, "1h", pd.Timestamp("2026-09-01T10:00Z"), pd.Timestamp("2026-09-01T12:00Z")) is None


@pytest.mark.parametrize("tf,opened,close", [
    ("1h", "2026-09-01T15:00Z", "2026-09-01T16:00Z"),
    ("4h", "2026-09-01T13:00Z", "2026-09-01T16:00Z"),
    ("1D", "2026-09-01T09:00Z", "2026-09-01T16:00Z"),
    ("1W", "2026-09-07T09:00Z", "2026-09-11T16:00Z"),
])
@pytest.mark.parametrize("delay", [5, 15])
def test_boundary_requires_delay_and_fresh_target_for_all_timeframes(tmp_path, tf, opened, close, delay):
    cal = calendar(tmp_path)
    deadline = pd.Timestamp(close) + pd.Timedelta(minutes=delay)
    f = with_evidence(cal, tf, opened)
    assert evaluate(f, tf, deadline - pd.Timedelta(seconds=1), delay).completion_reason == "awaiting_boundary_delay"
    assert evaluate(f, tf, deadline, delay).completion_reason == "awaiting_fresh_data"
    f = with_evidence(cal, tf, opened, deadline.isoformat())
    assert evaluate(f, tf, deadline, delay).is_bar_complete
    # A stale receipt cannot authenticate a subsequently changed value.
    f.iloc[-1, f.columns.get_loc("close")] = 100.5
    assert not evaluate(f, tf, deadline, delay).is_bar_complete


def test_intraday_mx1_regression_and_successor_confirmation(tmp_path):
    f = with_evidence(calendar(tmp_path), "1h", "2026-09-01T10:00Z", "2026-09-01T11:05Z")
    assert not evaluate(f, "1h", "2026-09-01T11:05Z").is_bar_complete
    assert not evaluate(f, "1h", "2026-09-01T15:00Z").is_bar_complete
    f.attrs["completion_context"]["raw_opens"].append("2026-09-01T11:00Z")
    assert evaluate(f, "1h", "2026-09-01T11:03Z").completion_reason == "successor_received"
    assert not evaluate(f, "1h", "2026-09-01T10:59Z").is_bar_complete


def test_normalized_daily_label_does_not_change_physical_boundary(tmp_path):
    f = with_evidence(calendar(tmp_path), "1D", "2026-09-01T09:00Z", "2026-09-01T16:05Z")
    f["source_timestamp"] = [f.index[0].isoformat()]
    f.index = pd.DatetimeIndex(["2026-09-01T00:00Z"])
    assert evaluate(f, "1D", "2026-09-01T16:05Z").is_bar_complete
    assert not evaluate(f, "1D", "2026-09-01T08:00Z").is_bar_complete


def test_calendar_has_separate_weekends_and_no_historical_lookahead(tmp_path):
    cal = learn_calendar(ASSET, native_frames(True), configured(tmp_path), pd.Timestamp("2026-09-01T00:00Z"))
    assert cal["session_templates"]["5"] != cal["session_templates"]["1"]
    assert expected_boundary(cal, "1h", pd.Timestamp("2026-09-05T12:00Z"), pd.Timestamp("2026-09-05T14:00Z")) == pd.Timestamp("2026-09-05T13:00Z")
    assert expected_boundary(cal, "1D", pd.Timestamp("2026-08-25T09:00Z"), pd.Timestamp("2026-08-25T17:00Z")) is None


def test_continuous_market_has_no_midnight_break_exception(tmp_path):
    frames = native_frames()
    frames["1h"] = frame("2026-07-01", [100.] * 1500)
    cal = learn_calendar(ASSET, frames, configured(tmp_path), pd.Timestamp("2026-09-01T00:00Z"))
    assert cal["status"] == "no_confirmed_breaks"
    assert not cal["rules"]


def test_conflicting_runtime_data_suspends_without_rebuilding(tmp_path):
    cal = calendar(tmp_path)
    version = cal["version"]
    frames = native_frames()
    frames["1h"] = frame("2026-09-01T15:00Z", [100.])
    assert suspend_conflicts(cal, frames, frame("2026-09-01T16:00Z", [100.]))
    assert cal["version"] == version
    assert expected_boundary(cal, "1h", pd.Timestamp("2026-09-01T15:00Z"), pd.Timestamp("2026-09-01T16:05Z")) is None


def test_asset_override_wins_over_exchange_and_alias(tmp_path):
    cfg = configured(tmp_path, finalization_delay_by_exchange={"RUS": 15}, finalization_delay_by_asset={"RUS:MX1!": 7})
    assert finalization_delay(cfg, "MX1!:RUS") == pd.Timedelta(minutes=7)
    assert finalization_delay(cfg, "RUS:SI1!") == pd.Timedelta(minutes=15)


class Provider:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def get_history(self, asset, timeframe, n_bars):
        self.calls.append((asset, timeframe, n_bars))
        if timeframe == self.fail:
            raise RuntimeError("temporary failure")
        return native_frames()[timeframe].copy()


def test_exchange_timezone_initializes_sber_and_rebuilds_on_restart(tmp_path):
    cfg = replace(make_settings(tmp_path), calendar_timezones_by_exchange={"RUS": "Europe/Moscow"})
    provider = Provider()
    storage = CsvStorage(cfg)
    sync = HistorySynchronizer(cfg, provider, storage)
    sync.ensure_available("sber:rus", "1h", pd.Timestamp("2026-08-01T00:00Z"))
    assert provider.calls == [("RUS:SBER", tf, cfg.initial_bars) for tf in ("1h", "4h", "1D", "1W")]
    assert sync.calendars["RUS:SBER"]["timezone"] == "Europe/Moscow"
    assert not (cfg.timestamp_profiles or {}).get("RUS:SBER")
    assert "RUS:SBER" not in cfg.rus_daily_trading_date_assets
    # Existing caches also pick up updated defaults at launch without downloads.
    cfg = replace(cfg, calendar_timezones_by_exchange={"RUS": "UTC"})
    restarted_provider = Provider()
    restarted = HistorySynchronizer(cfg, restarted_provider, storage)
    assert restarted.calendars["RUS:SBER"]["timezone"] == "UTC"
    assert restarted_provider.calls == []


@pytest.mark.parametrize("asset,profile,override,expected", [
    ("RUS:SBER", None, None, "Europe/Moscow"),
    ("RUS:SBER", None, "Europe/London", "Europe/London"),
    ("RUS:SBER", "utc_calendar", None, "UTC"),
    ("RUS:SBER", "utc_calendar", "Europe/London", "Europe/London"),
    ("RUS:MX1!", None, None, "Europe/Moscow"),
    ("NASDAQ:ABC", None, None, None),
])
def test_calendar_timezone_precedence(tmp_path, asset, profile, override, expected):
    cfg = replace(make_settings(tmp_path),
                  timestamp_profiles={asset: profile} if profile else {},
                  calendar_timezones={asset: override} if override else {},
                  calendar_timezones_by_exchange={"RUS": "Europe/Moscow"})
    cal = learn_calendar(asset, native_frames(), cfg, pd.Timestamp("2026-09-01T00:00Z"))
    assert cal["timezone"] == expected
    if expected:
        assert cal["status"] == "ready"


def test_config_validates_exchange_timezone_defaults(tmp_path):
    import yaml
    from zoneinfo import ZoneInfoNotFoundError
    from tv_history.config import load_settings
    path = tmp_path / "config.yaml"
    content = {"storage": {"root": "data"}, "provider": {}, "indicators": {},
               "calendar": {"timezones_by_exchange": {" rus ": "Europe/Moscow"}}}
    path.write_text(yaml.safe_dump(content))
    assert load_settings(path).calendar_timezones_by_exchange == {"RUS": "Europe/Moscow"}
    for mapping, error in [
        ({"RUS": "Invalid/Timezone"}, ZoneInfoNotFoundError),
        ({"RUS:SBER": "Europe/Moscow"}, ValueError),
        ({"RUS": "UTC", "rus": "Europe/Moscow"}, ValueError),
    ]:
        content["calendar"]["timezones_by_exchange"] = mapping
        path.write_text(yaml.safe_dump(content))
        with pytest.raises(error):
            load_settings(path)


def test_new_ticker_full_batch_is_shared_persisted_and_calendar_frozen(tmp_path):
    cfg = configured(tmp_path, initial_bars=1234)
    provider = Provider()
    sync = HistorySynchronizer(cfg, provider, CsvStorage(cfg))
    cutoff = pd.Timestamp("2026-08-01T00:00Z")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda a: sync.ensure_available(a, "4h", cutoff), [ASSET, "ABC:NASDAQ"]))
    assert provider.calls == [(ASSET, tf, 1234) for tf in ("1h", "4h", "1D", "1W")]
    version = sync.calendars[ASSET]["version"]
    sync.ensure_available(ASSET, "1h", pd.Timestamp("2026-09-01T00:00Z"))
    assert sync.calendars[ASSET]["version"] == version
    restarted_provider = Provider()
    restarted = HistorySynchronizer(cfg, restarted_provider, CsvStorage(cfg))
    assert restarted.calendars[ASSET]["version"] != version
    restarted.ensure_available(ASSET, "4h", cutoff)
    assert restarted_provider.calls == []


def test_failed_batch_retries_only_missing_timeframes_even_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("tv_history.sync.time.sleep", lambda _: None)
    cfg = configured(tmp_path)
    provider = Provider(fail="1D")
    sync = HistorySynchronizer(cfg, provider, CsvStorage(cfg))
    with pytest.raises(RuntimeError):
        sync.ensure_available(ASSET, "1W", pd.Timestamp("2026-08-01T00:00Z"))
    assert [n for _, tf, n in provider.calls if tf == "1D"] == [5000, 5000, 5000, 4000, 2000]
    assert read_json(cfg.data_root / "ticker_registry.json", {})[ASSET] == ["1h", "4h"]
    provider = Provider()
    sync = HistorySynchronizer(cfg, provider, CsvStorage(cfg))
    sync.ensure_available(ASSET, "1W", pd.Timestamp("2026-08-01T00:00Z"))
    assert [tf for _, tf, _ in provider.calls] == ["1D", "1W"]


def test_receipts_and_revisions_preserve_original_values(tmp_path):
    store = CsvStorage(configured(tmp_path))
    original = frame("2026-09-09T07:00Z", [227750., 229000.])
    store.merge_and_write(ASSET, original, "1h", pd.Timestamp("2026-09-09T08:05Z"))
    updated = frame("2026-09-09T07:00Z", [228850.])
    store.merge_and_write(ASSET, updated, "1h", pd.Timestamp("2026-09-09T09:05Z"))
    path = store.path_for(ASSET)
    assert '227750.0' in (path.parent / 'revisions.jsonl').read_text()
    assert store.read(ASSET).attrs['receipts'][original.index[0].isoformat()]['ohlcv'][3] == 228850.


def test_unknown_timezone_and_short_history_cannot_finalize_by_clock(tmp_path):
    frames = native_frames()
    unknown = learn_calendar(ASSET, frames, make_settings(tmp_path), pd.Timestamp("2026-09-01T00:00Z"))
    assert unknown["status"] == "insufficient_history"
    short = {tf: f.loc[f.index >= "2026-08-25"] for tf, f in frames.items()}
    cal = learn_calendar(ASSET, short, configured(tmp_path), pd.Timestamp("2026-09-01T00:00Z"))
    assert cal["eligible_sessions"] < 30 and not cal["rules"]


def test_lunch_break_does_not_count_as_two_days_or_end_spanning_four_hour_bar(tmp_path):
    frames = native_frames()
    frames["1h"] = frames["1h"].loc[frames["1h"].index.hour != 11]
    cal = learn_calendar(ASSET, frames, configured(tmp_path), pd.Timestamp("2026-09-01T00:00Z"))
    assert cal["eligible_sessions"] == 30
    assert pd.Timestamp(cal["training_through"]) - pd.Timestamp(cal["training_from"]) > pd.Timedelta(days=35)
    # Native 09:00 four-hour candle also contains the 12:00 bar after lunch.
    assert expected_boundary(cal, "4h", pd.Timestamp("2026-09-01T09:00Z"), pd.Timestamp("2026-09-01T11:05Z")) is None
    assert expected_boundary(cal, "1h", pd.Timestamp("2026-09-01T10:00Z"), pd.Timestamp("2026-09-01T11:05Z")) == pd.Timestamp("2026-09-01T11:00Z")


def test_dst_boundary_uses_exchange_wall_time():
    assert boundary_from_rule(pd.Timestamp("2026-03-07T23:00Z"), (1, 16, 0), "America/Chicago") == pd.Timestamp("2026-03-08T21:00Z")


def test_config_normalizes_asset_delays_and_date_override(tmp_path):
    import yaml
    from tv_history.config import load_settings
    path = tmp_path / "config.yaml"
    content = {"storage": {"root": "data"}, "indicators": {},
               "provider": {"finalization_delay_by_asset": {"ABC:NASDAQ": 12}},
               "calendar": {"boundary_overrides": {ASSET: {"1D": {
                   "2026-09-01T09:00Z": "2026-09-01T13:00Z"}}}}}
    path.write_text(yaml.safe_dump(content))
    cfg = load_settings(path)
    assert cfg.finalization_delay_by_asset == {ASSET: 12}
    cal = learn_calendar(ASSET, native_frames(), cfg, pd.Timestamp("2026-09-01T00:00Z"))
    assert expected_boundary(cal, "1D", pd.Timestamp("2026-09-01T09:00Z"), pd.Timestamp("2026-09-01T13:12Z")) == pd.Timestamp("2026-09-01T13:00Z")
    content["calendar"]["boundary_overrides"][ASSET]["1D"] = {"2026-09-01T09:00": "2026-09-01T13:00Z"}
    path.write_text(yaml.safe_dump(content))
    with pytest.raises(ValueError, match="aware timestamps"):
        load_settings(path)


def test_overnight_daily_period_ends_on_following_date(tmp_path):
    stamps = {tf: [] for tf in ("1h", "4h", "1D", "1W")}
    for day in pd.date_range("2026-06-01", "2026-08-31", tz="UTC"):
        if day.weekday() in (4, 5):
            continue
        start = day + pd.Timedelta(hours=22)
        stamps["1h"].extend(start + pd.Timedelta(hours=h) for h in range(23))
        stamps["4h"].extend(start + pd.Timedelta(hours=h) for h in range(0, 23, 4))
        stamps["1D"].append(start)
        if day.weekday() == 6:
            stamps["1W"].append(start)
    frames = {tf: pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100., "volume": 100.},
                               index=pd.DatetimeIndex(times)) for tf, times in stamps.items()}
    cfg = replace(configured(tmp_path), calendar_timezones={ASSET: "America/Chicago"})
    cal = learn_calendar(ASSET, frames, cfg, pd.Timestamp("2026-09-01T22:00Z"))
    assert expected_boundary(cal, "1D", pd.Timestamp("2026-09-02T22:00Z"), pd.Timestamp("2026-09-03T21:15Z")) == pd.Timestamp("2026-09-03T21:00Z")


def test_bars_and_chart_include_verified_short_final_four_hour_candle(tmp_path, monkeypatch):
    from tv_history.bars import AssetBarsService
    from tv_history.chart import AssetChartService
    from test_analysis import settings
    cfg = replace(settings(tmp_path), calendar_timezones={ASSET: "UTC"})
    f = with_evidence(calendar(tmp_path), "4h", "2026-09-01T13:00Z", "2026-09-01T16:05:01Z")

    class Direct:
        def ensure_available(self, *args, **kwargs):
            return f, {}

    # The fresh response arrives after requested_at; its period is already closed.
    monkeypatch.setattr("tv_history.chart.render_candles", lambda *args: b"PNG")
    bars = AssetBarsService(cfg, Direct()).get_bars(ASSET, "4h", "2026-09-01T16:05:00Z")
    assert bars["bars"][-1]["is_bar_complete"] is True
    chart = AssetChartService(cfg, Direct(), CsvStorage(cfg)).render(ASSET, "4h", "2026-09-01T16:05:00Z", 1)
    assert chart.metadata["is_bar_complete"] is True
    assert chart.metadata["completion_boundary"] == "2026-09-01T16:00:00+00:00"
    assert chart.metadata["completion_reason"] == "session_boundary_delay"


def test_finality_does_not_copy_evidence_for_every_bar():
    class CopyProbe:
        calls = 0

        def __deepcopy__(self, memo):
            self.calls += 1
            return self

    f = frame("2026-09-01T00:00Z", [100.] * 200)
    probe = CopyProbe()
    f.attrs["probe"] = probe
    result = completion_details(f, pd.Timedelta(hours=1), pd.Timestamp("2026-09-10T00:00Z"), pd.Timedelta(minutes=5))
    assert result.is_bar_complete.sum() == 199
    assert probe.calls < 5

import pandas as pd
import pytest

from tv_history.rollover_adjustment import RolloverAdjustments, rollover_view
from tv_history.storage import CsvStorage
from test_analysis import settings, FakeSynchronizer

ASSET = "ICEEUR:BRN1!"
SWITCH = pd.Timestamp("2026-09-24T00:00Z")
END = pd.Timestamp("2026-09-25T22:00Z")


def setup_case(tmp_path, stamp=SWITCH, basis="1h"):
    cfg = settings(tmp_path)
    store = CsvStorage(cfg)
    source = pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100., "volume": 10.},
                         index=pd.date_range("2026-09-20T22:00Z", "2026-09-28T01:00Z", freq="1h"))
    source.loc[source.index >= stamp, ["open", "high", "low", "close"]] -= 5
    calendar = {"effective_from": "2026-09-01T00:00:00+00:00", "status": "insufficient_history", "rules": {},
                "trading_weeks": {"schema_version": 2, "status": "ready", "timezone": "Europe/London",
                "weekend_belongs_to_next_week": True, "open": {"day_offset": -1, "time": "23:00"},
                "close": {"day_offset": 4, "time": "23:00"}}}
    source.attrs["completion_context"] = {"calendar": calendar, "timeframe": "1h", "raw_opens": list(source.index)}
    store.save_rollovers(ASSET, {"events": [{"from": "ICEEUR:BRNX2026", "to": "ICEEUR:BRNZ2026",
        "scheduled_at_utc": stamp.isoformat(), "timestamp_basis": basis}]})
    store.merge_and_write(ASSET, source)
    engine = RolloverAdjustments(store)
    sync = FakeSynchronizer(source)
    sync.rollover_adjustments = engine
    return cfg, store, source, sync, calendar


def test_only_rollover_week_ohlc_changes_and_offset_survives_restart(tmp_path):
    _, store, source, sync, _ = setup_case(tmp_path)
    raw = source.copy(deep=True)
    path = store.path_for(ASSET)
    before = path.read_bytes()
    result, metadata = rollover_view(sync, source, source, ASSET, "1h", source.index[-1], True)
    mask = (source.index >= SWITCH) & (source.index < END)
    for col in ("open", "high", "low", "close"):
        pd.testing.assert_series_equal(result[col], source[col] + mask.astype(int) * 5)
    pd.testing.assert_series_equal(result.volume, source.volume)
    pd.testing.assert_frame_equal(raw, source)
    assert path.read_bytes() == before
    assert metadata["events"][0]["offset"] == 5
    assert metadata["latest_bar_offset"] == 0
    source.loc[SWITCH, "open"] = 90  # later source revision must not change the saved offset
    sync.rollover_adjustments = RolloverAdjustments(store)
    _, replay = rollover_view(sync, source, source.loc[[SWITCH]], ASSET, "1h", SWITCH, True)
    assert replay["events"][0]["offset"] == 5


@pytest.mark.parametrize("timeframe,enabled", [("1h", False), ("4h", True), ("1D", True), ("1W", True)])
def test_disabled_and_other_timeframes_remain_raw(tmp_path, timeframe, enabled):
    _, store, source, sync, _ = setup_case(tmp_path)
    result, metadata = rollover_view(sync, source, source, ASSET, timeframe, source.index[-1], enabled)
    pd.testing.assert_frame_equal(result, source)
    assert not metadata["applied"]
    assert store.read_rollover_adjustments(ASSET) == {}


def test_daily_anchor_and_future_event_do_not_adjust_or_freeze(tmp_path):
    _, store, source, sync, _ = setup_case(tmp_path, basis="1D")
    result, metadata = rollover_view(sync, source, source, ASSET, "1h", source.index[-1], True)
    assert metadata["events"][0]["reason"] == "awaiting_hourly_rollover_confirmation"
    pd.testing.assert_frame_equal(result, source)
    assert store.read_rollover_adjustments(ASSET) == {}
    _, metadata = rollover_view(sync, source, source.loc[source.index < SWITCH], ASSET, "1h", SWITCH - pd.Timedelta(hours=1), True)
    assert metadata["events"][0]["status"] == "scheduled"


def test_week_open_and_missing_history_are_not_adjusted(tmp_path):
    next_week = pd.Timestamp("2026-09-27T22:00Z")
    _, _, source, sync, _ = setup_case(tmp_path, stamp=next_week)
    source = source.loc[(source.index < END) | (source.index >= next_week)]
    _, metadata = rollover_view(sync, source, source, ASSET, "1h", source.index[-1], True)
    assert metadata["events"][0]["reason"] == "rollover_between_weeks"
    _, _, source, sync, _ = setup_case(tmp_path / "missing")
    source = source.loc[source.index >= SWITCH]
    _, metadata = rollover_view(sync, source, source, ASSET, "1h", source.index[-1], True)
    assert metadata["events"][0]["reason"] == "rollover_or_previous_bar_missing"


def test_only_bars_adjusts_while_analysis_and_chart_remain_raw(tmp_path, monkeypatch):
    from tv_history.bars import AssetBarsService
    from tv_history.analysis import AssetAnalysisService
    from tv_history.chart import AssetChartService, ChartResult
    cfg, store, source, sync, _ = setup_case(tmp_path)
    cutoff = "2026-09-24T03:30Z"
    bars = AssetBarsService(cfg, sync)
    on = bars.get_bars(ASSET, "1h", cutoff, 3, rollover=True)
    off = bars.get_bars(ASSET, "1h", cutoff, 3)
    assert "bars" in on, on
    assert on["bars"][-1]["c"] == off["bars"][-1]["c"] + 5
    assert [b["is_bar_complete"] for b in on["bars"]] == [b["is_bar_complete"] for b in off["bars"]]
    rendered = []
    monkeypatch.setattr("tv_history.chart.render_candles", lambda frame, meta: (rendered.append(frame.copy()) or b"png"))
    chart = AssetChartService(cfg, sync, store)
    result = chart.render(ASSET, "1h", cutoff, 1)
    assert isinstance(result, ChartResult) and "rollover" not in result.metadata
    assert rendered[-1].iloc[-1].close == 95
    captured = []
    def response(*args):
        captured.append(args)
        return {}
    monkeypatch.setattr("tv_history.execution.build_execution_response", response)
    analysis = AssetAnalysisService(cfg, sync)
    response_raw = analysis.analyze(ASSET, "1h", cutoff)
    assert "rollover" not in response_raw
    assert captured[0][5].iloc[-1].close == 95
    # The bars request has already frozen a gap; other services must never use it.
    def unexpected(*args, **kwargs):
        pytest.fail("analysis/chart accessed rollover adjustment")
    monkeypatch.setattr(sync.rollover_adjustments, "apply", unexpected)
    assert "rollover" not in analysis.analyze(ASSET, "1h", cutoff)
    assert "rollover" not in chart.render(ASSET, "1h", cutoff, 1).metadata


def test_raw_receipt_finality_is_preserved_on_adjusted_last_bar(tmp_path, monkeypatch):
    from tv_history.bars import AssetBarsService
    cfg, store, source, sync, calendar = setup_case(tmp_path)
    last = END - pd.Timedelta(hours=1)
    sync.frame = source.loc[source.index <= last].copy()
    context = sync.frame.attrs["completion_context"]
    context["raw_opens"] = list(sync.frame.index)
    calendar.update(status="ready", timezone="Europe/London",
                    rules={"1h|4:22:00": {"offset": [0, 23, 0]}})
    context["calendar"] = calendar
    context["receipts"] = {last.isoformat(): {"request_started_at": (END + pd.Timedelta(minutes=20)).isoformat(),
        "ohlcv": [float(sync.frame.iloc[-1][k]) for k in ("open", "high", "low", "close", "volume")]}}
    cutoff = END + pd.Timedelta(minutes=30)
    monkeypatch.setattr("tv_history.bars.current_utc_time", lambda: cutoff)
    result = AssetBarsService(cfg, sync).get_bars(ASSET, "1h", cutoff.isoformat(), 1, rollover=True)
    assert result["bars"][0]["is_bar_complete"]
    assert result["bars"][0]["completion_reason"] == "session_boundary_delay"
    assert result["bars"][0]["c"] == 100


def test_two_rollovers_accumulate_within_week(tmp_path):
    _, store, source, sync, _ = setup_case(tmp_path)
    second = SWITCH + pd.Timedelta(hours=12)
    source.loc[source.index >= second, ["open", "high", "low", "close"]] -= 3
    schedule = store.read_rollovers(ASSET)
    schedule["events"].append({"from": "ICEEUR:BRNZ2026", "to": "ICEEUR:BRNF2027",
        "scheduled_at_utc": second.isoformat(), "timestamp_basis": "1h"})
    store.save_rollovers(ASSET, schedule)
    result, metadata = rollover_view(sync, source, source.loc[[SWITCH, second, END]], ASSET, "1h", END, True)
    assert result.close.tolist() == [100, 100, 92]
    assert [e["offset"] for e in metadata["events"]] == [5, 8]


def test_only_bars_exposes_rollover_with_renamed_end():
    import asyncio
    from tv_history.server import mcp
    tools = asyncio.run(mcp.list_tools())
    assert len(tools) == 3
    for tool in tools:
        properties = tool.inputSchema["properties"]
        assert "rollover_end_week" not in properties
        if tool.name != "asset_bars":
            assert not any(name.startswith("rollover") for name in properties)
            assert "rollover" not in tool.description
            continue
        parameter = properties["rollover"]
        assert parameter["type"] == "boolean" and parameter["default"] is False
        assert "rollover" in tool.description
        for name in ("rollover_start", "rollover_end"):
            assert tool.inputSchema["properties"][name]["default"] is None


@pytest.mark.parametrize("status,applied", [("uncertain", True), ("incomplete", False)])
def test_session_break_is_allowed_but_confirmed_missing_bars_block(tmp_path, status, applied):
    _, _, source, sync, _ = setup_case(tmp_path)
    previous = SWITCH - pd.Timedelta(hours=2)
    source = source.drop(SWITCH - pd.Timedelta(hours=1))
    source.attrs["history_coverage"] = {"unresolved_gaps": [{"after": previous.isoformat(),
        "before": SWITCH.isoformat(), "status": status}]}
    _, metadata = rollover_view(sync, source, source, ASSET, "1h", source.index[-1], True)
    assert metadata["applied"] == applied


def extended_case(tmp_path):
    cfg, store, source, sync, calendar = setup_case(tmp_path)
    extra = pd.DataFrame({"open": 95., "high": 96., "low": 94., "close": 95., "volume": 10.},
        index=pd.date_range(source.index[-1] + pd.Timedelta(hours=1), "2026-10-03T00:00Z", freq="1h"))
    attrs = source.attrs
    source.attrs = {}
    source = pd.concat([source, extra])
    source.attrs = attrs
    source.attrs["completion_context"]["raw_opens"] = list(source.index)
    sync.frame = source
    return cfg, store, source, sync, calendar


@pytest.mark.parametrize("start,end_week,at_roll,at_next_week", [
    (None, None, 100, 95),
    ("2026-09-23T08:00:00Z", None, 100, 95),
    ("2026-09-24T01:00:00Z", None, 95, 95),
    (None, "2026-09-21", 100, 95),
    (None, "2026-09-28", 95, 95),
    ("2026-09-23T08:00:00Z", "2026-09-28", 100, 100),
    ("2026-09-24T00:00:00Z", "2026-09-28", 100, 100),
])
def test_optional_bounds_and_exact_start(tmp_path, start, end_week, at_roll, at_next_week):
    _, _, source, sync, _ = extended_case(tmp_path)
    adjusted, meta = rollover_view(sync, source, source, ASSET, "1h", source.index[-1], True, start, end_week)
    assert adjusted.loc[SWITCH, "close"] == at_roll
    assert adjusted.loc["2026-09-28T00:00Z", "close"] == at_next_week
    assert adjusted.loc["2026-10-02T22:00Z", "close"] == 95  # end exclusive
    pd.testing.assert_frame_equal(adjusted.loc[adjusted.index < SWITCH], source.loc[source.index < SWITCH])
    if end_week:
        assert meta["resolved_end_utc"] == ("2026-09-25T22:00:00+00:00" if end_week == "2026-09-21" else "2026-10-02T22:00:00+00:00")
    if end_week and not start:
        assert meta["resolved_start_utc"] == ("2026-09-20T22:00:00+00:00" if end_week == "2026-09-21" else "2026-09-27T22:00:00+00:00")


def test_explicit_episode_includes_week_open_but_default_still_skips(tmp_path):
    stamp = pd.Timestamp("2026-09-27T22:00Z")
    _, store, source, sync, _ = setup_case(tmp_path, stamp=stamp)
    source = source.loc[(source.index < END) | (source.index >= stamp)]
    adjusted, meta = rollover_view(sync, source, source, ASSET, "1h", source.index[-1], True, None, "2026-09-28")
    assert adjusted.loc[stamp, "close"] == 100
    assert meta["events"][0]["between_weeks"]
    raw, meta = rollover_view(sync, source, source, ASSET, "1h", source.index[-1], True)
    pd.testing.assert_frame_equal(raw, source)
    assert meta["events"][0]["reason"] == "rollover_between_weeks"


def test_excluded_first_switch_does_not_leak_saved_cumulative_offset(tmp_path):
    _, store, source, sync, _ = extended_case(tmp_path)
    second = pd.Timestamp("2026-09-28T10:00Z")
    source.loc[source.index >= second, ["open", "high", "low", "close"]] -= 3
    schedule = store.read_rollovers(ASSET)
    schedule["events"].append({"from": "ICEEUR:BRNZ2026", "to": "ICEEUR:BRNF2027",
        "scheduled_at_utc": second.isoformat(), "timestamp_basis": "1h"})
    store.save_rollovers(ASSET, schedule)
    adjusted, meta = rollover_view(sync, source, source, ASSET, "1h", source.index[-1], True,
                                  "2026-09-23T00:00Z", "2026-09-28")
    assert adjusted.loc[second, "close"] == 100
    assert [e["offset"] for e in meta["events"]] == [5, 8]
    saved = store.read_rollover_adjustments(ASSET)
    assert saved["schema_version"] == 2
    assert all("offset" not in r and "applied_until_utc" not in r for r in saved["events"].values())
    # Simulate the older persisted cumulative format; only raw gap may be reused.
    saved["schema_version"] = 1
    for record in saved["events"].values():
        record.update(offset=12345, applied_until_utc="2026-09-25T22:00:00+00:00")
    store.save_rollover_adjustments(ASSET, saved)
    sync.rollover_adjustments = RolloverAdjustments(store)
    adjusted, meta = rollover_view(sync, source, source, ASSET, "1h", source.index[-1], True,
                                  "2026-09-25T00:00Z", "2026-09-28")
    assert adjusted.loc[SWITCH, "close"] == 95
    assert adjusted.loc[second, "close"] == 95  # second raw 92 + independent gap 3
    assert len(meta["events"]) == 1 and meta["events"][0]["offset"] == 3


def test_explicit_end_excludes_rollover_at_close_and_dst_end(tmp_path):
    _, _, source, sync, calendar = setup_case(tmp_path, stamp=END)
    raw, meta = rollover_view(sync, source, source, ASSET, "1h", source.index[-1], True,
                            "2026-09-23T00:00Z", "2026-09-21")
    pd.testing.assert_frame_equal(raw, source)
    assert meta["events"] == []
    _, meta = rollover_view(sync, source, source, ASSET, "1h", source.index[-1], True,
                           "2026-09-23T00:00Z", "2026-10-26")
    assert meta["resolved_end_utc"] == "2026-10-30T23:00:00+00:00"
    assert meta["resolved_end_local"] == "2026-10-30T23:00:00+00:00"


@pytest.mark.parametrize("start,end", [("2026-09-23", None), ("bad", None),
    (None, "2026-09-22"), (None, "2026-09-21T00:00Z"), ("2026-09-26T00:00Z", "2026-09-21")])
def test_invalid_bounds_return_parameter_error_on_bars(tmp_path, start, end):
    from tv_history.bars import AssetBarsService
    cfg, store, _, sync, _ = setup_case(tmp_path)
    options = dict(rollover=True, rollover_start=start, rollover_end=end)
    result = AssetBarsService(cfg, sync).get_bars(ASSET, "1h", "2026-09-24T03:30Z", **options)
    assert result.get("error", {}).get("code") == "INVALID_PARAMETER", result


def test_bars_forwards_episode_bounds(tmp_path, monkeypatch):
    from tv_history.bars import AssetBarsService
    cfg, store, source, sync, _ = extended_case(tmp_path)
    cutoff = "2026-09-28T00:30Z"
    monkeypatch.setattr("tv_history.bars.current_utc_time", lambda: source.index[-1])
    options = dict(rollover=True, rollover_start="2026-09-23T00:00Z", rollover_end="2026-09-28")
    bars = AssetBarsService(cfg, sync).get_bars(ASSET, "1h", cutoff, **options)
    meta = bars["rollover"]
    assert meta["mode"] == "episode"
    assert meta["latest_bar_offset"] == 5
    assert meta["resolved_end_utc"] == "2026-10-02T22:00:00+00:00"
    assert meta["rollover_end"] == "2026-09-28"
    assert "rollover_end_week" not in meta


def test_episode_parameters_do_not_change_disabled_or_other_timeframes(tmp_path):
    _, store, source, sync, _ = setup_case(tmp_path)
    for tf, enabled in [("1h", False), ("4h", True), ("1D", True), ("1W", True)]:
        raw, meta = rollover_view(sync, source, source, ASSET, tf, source.index[-1], enabled,
                                  "2026-09-23T00:00Z", "2026-09-28")
        pd.testing.assert_frame_equal(raw, source)
        assert not meta["applied"]
    assert store.read_rollover_adjustments(ASSET) == {}


def test_episode_future_switch_uses_no_future_prices_and_unknown_calendar_errors(tmp_path):
    from tv_history.rollover_adjustment import RolloverParameterError
    _, store, source, sync, calendar = setup_case(tmp_path)
    cutoff = SWITCH - pd.Timedelta(hours=1)
    selected = source.loc[source.index <= cutoff]
    raw, meta = rollover_view(sync, source, selected, ASSET, "1h", cutoff, True,
                              "2026-09-23T00:00Z", "2026-09-28")
    pd.testing.assert_frame_equal(raw, selected)
    assert meta["events"][0]["status"] == "scheduled"
    assert store.read_rollover_adjustments(ASSET) == {}
    calendar["trading_weeks"]["status"] = "unavailable"
    with pytest.raises(RolloverParameterError, match="calendar unavailable"):
        rollover_view(sync, source, selected, ASSET, "1h", cutoff, True, None, "2026-09-28")

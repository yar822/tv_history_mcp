"""Historical session and completion boundaries must agree across markets."""
import json

import pandas as pd
import pytest

from tv_history.bars import AssetBarsService
from tv_history.finality import completion_details
from tv_history.storage import CsvStorage
from tv_history.sync import HistorySynchronizer
from tv_history.trading_session import TradingSessionService
from test_analysis import FakeSynchronizer, settings


def example(tmp_path, asset="ICEEUR:BRN1!", zone="Europe/London", weekend=True,
            end="2025-10-31T22:00Z", gaps=None):
    cfg = settings(tmp_path)
    storage = CsvStorage(cfg)
    calendar = {"schema_version": 2, "status": "ready", "timezone": zone,
                "effective_from": "2026-09-01T00:00Z", "trading_weeks": {
                    "schema_version": 2, "status": "ready", "timezone": zone,
                    "weekend_belongs_to_next_week": weekend,
                    "open": {"day_offset": -1 if weekend else 0, "time": "23:00" if weekend else "00:00"},
                    "close": {"day_offset": 4 if weekend else 7, "time": "23:00" if weekend else "00:00"}}}
    close = pd.Timestamp(end)
    start = pd.Timestamp("2025-10-27T06:00Z")
    index = pd.DatetimeIndex([pd.Timestamp("2025-10-23T12:00Z"),
                             *pd.date_range(start, close, freq="h", inclusive="left"),
                             pd.Timestamp("2025-11-03T06:00Z")])
    raw = pd.DataFrame({k: 100. for k in ("open", "high", "low", "close", "volume")}, index=index)
    coverage = {"unresolved_gaps": gaps or []}
    folder = storage.path_for(asset).parent
    folder.mkdir(parents=True)
    raw.rename_axis("timestamp_utc").to_csv(folder / "1h.csv")
    (folder / "calendar.json").write_text(json.dumps(calendar), encoding="utf-8")
    (folder / "coverage.json").write_text(json.dumps({"1h": coverage}), encoding="utf-8")
    # Exercise the real synchronizer's propagation of full-cache evidence.
    sync = object.__new__(HistorySynchronizer)
    sync.settings, sync.calendars, sync.coverage = cfg, {asset: calendar}, {asset: {"1h": coverage}}
    source = sync._served_view(asset, "1h", raw, close)
    return storage, source, close, cfg


def detail(source, opened, cutoff):
    target = source.loc[[opened]].copy()
    return completion_details(target, pd.Timedelta(hours=1), cutoff, pd.Timedelta(minutes=5)).iloc[0]


@pytest.mark.parametrize("asset,zone,weekend,end", [
    ("ICEEUR:BRN1!", "Europe/London", True, "2025-10-31T22:00Z"),
    ("COMEX:GC1!", "America/Chicago", True, "2025-10-31T21:00Z"),
    ("RUS:MX1!", "Europe/Moscow", True, "2025-10-31T20:00Z"),
    ("BITSTAMP:BTCUSD", "UTC", False, "2025-11-03T00:00Z"),
])
def test_shared_historical_close_across_assets(tmp_path, monkeypatch, asset, zone, weekend, end):
    storage, source, close, cfg = example(tmp_path, asset, zone, weekend, end)
    now = pd.Timestamp("2025-11-10T12:00Z")
    monkeypatch.setattr("tv_history.finality.current_utc_time", lambda: now)
    session = TradingSessionService(storage, lambda: now).get_sessions(asset, date="2025-10-27")
    assert session["status"] == "observed"
    assert pd.Timestamp(session["sessions"][0]["close"]["datetime"]) == close
    opened = close - pd.Timedelta(hours=1)
    before = detail(source, opened, close - pd.Timedelta(seconds=1))
    assert not before.is_bar_complete
    at_close = detail(source, opened, close)
    assert at_close.is_bar_complete
    assert at_close.completion_reason == "historical_week_close_successor_confirmed"
    assert pd.Timestamp(at_close.completion_boundary) == close
    # Later successor prices must not enter the response or its ATR.
    source.loc[source.index[-1], ["open", "high", "low", "close"]] = 9000.
    response = AssetBarsService(cfg, FakeSynchronizer(source)).get_bars(asset, "1h", close.isoformat(), count=20)
    assert response["bars"][-1]["is_bar_complete"] is True
    assert all(bar["c"] == 100. and pd.Timestamp(bar["t"]) < close for bar in response["bars"])
    assert response["atr_14"] == 0.


@pytest.mark.parametrize("case", ["no_previous", "no_next", "unknown_coverage", "missing_tail", "missing_inside", "ongoing", "future"])
def test_unsafe_observed_boundary_does_not_confirm(tmp_path, monkeypatch, case):
    storage, source, close, _ = example(tmp_path)
    now = pd.Timestamp("2025-11-10T12:00Z")
    context = source.attrs["completion_context"]
    if case == "no_previous":
        context["raw_opens"] = context["raw_opens"][1:]
    elif case == "no_next":
        context["raw_opens"] = context["raw_opens"][:-1]
    elif case == "unknown_coverage":
        context["hourly_coverage"] = {}
    elif case in ("missing_tail", "missing_inside"):
        context["hourly_coverage"] = {"unresolved_gaps": [{
            "after": "2025-10-31T21:00Z" if case == "missing_tail" else "2025-10-28T12:00Z",
            "before": "2025-11-03T06:00Z", "status": "incomplete"}]}
    elif case == "ongoing":
        now = pd.Timestamp("2025-10-31T23:30Z")
    else:
        now = pd.Timestamp("2025-10-20T12:00Z")
    monkeypatch.setattr("tv_history.finality.current_utc_time", lambda: now)
    assert not detail(source, close - pd.Timedelta(hours=1), close).is_bar_complete
    if case == "ongoing":
        session = TradingSessionService(storage, lambda: now).get_sessions("ICEEUR:BRN1!", date="2025-10-27")
        assert session["status"] == "expected"
        assert session["metadata"]["week_state"] == "ongoing"


def test_uncertain_gap_preserves_observation_and_later_close_wins(tmp_path, monkeypatch):
    # The observed close can also be later than the learned close.
    storage, source, close, _ = example(tmp_path, end="2025-11-01T00:00Z", gaps=[{
        "after": "2025-10-28T12:00Z", "before": "2025-10-28T14:00Z", "status": "uncertain"}])
    now = pd.Timestamp("2025-11-10T12:00Z")
    monkeypatch.setattr("tv_history.finality.current_utc_time", lambda: now)
    session = TradingSessionService(storage, lambda: now).get_sessions("ICEEUR:BRN1!", date="2025-10-27")
    assert session["metadata"]["coverage"] == "uncertain"
    assert detail(source, close - pd.Timedelta(hours=1), close).is_bar_complete
    # At the obsolete learned close, the prior bar has its ordinary successor
    # in the cutoff; it is not marked as a historical week-ending bar.
    prior = detail(source, close - pd.Timedelta(hours=2), close - pd.Timedelta(hours=1))
    assert prior.completion_reason == "successor_received"

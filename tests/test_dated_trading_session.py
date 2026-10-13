from datetime import datetime
import json

import pandas as pd
import pytest

from tv_history.trading_session import TradingSessionService
from test_trading_session import setup_calendar


ASSET = "CME_MINI:NQ1!"


def prepared(tmp_path, zone="America/Chicago", now="2026-09-28T06:00:00+00:00"):
    storage, calendar, path = setup_calendar(tmp_path, zone)
    path.write_text(json.dumps(calendar), encoding="utf-8")
    service = TradingSessionService(storage, lambda: datetime.fromisoformat(now))
    return service, calendar, path


def history(path, stamps=None, gaps=None):
    if stamps is None:
        stamps = ["2025-09-19T20:00Z"]
        for day in pd.date_range("2025-09-21", periods=5):
            start = (day + pd.Timedelta(hours=17)).tz_localize("America/Chicago")
            stamps.extend(pd.date_range(start, periods=23, freq="h").tz_convert("UTC"))
        stamps.append("2025-09-28T22:00Z")
    index = pd.DatetimeIndex(pd.to_datetime(stamps, utc=True)).sort_values()
    pd.DataFrame({k: 1. for k in ("open", "high", "low", "close", "volume")},
                 index=index.rename("timestamp_utc")).to_csv(path.parent / "1h.csv")
    (path.parent / "coverage.json").write_text(json.dumps({"1h": {"unresolved_gaps": gaps or []}}), encoding="utf-8")


@pytest.mark.parametrize("date", ["2025-09-22", "2025-09-23", "2025-09-24", "2025-09-25",
                                  "2025-09-26", "2025-09-27", "2025-09-28"])
def test_normalize_all_days_to_same_monday_observed_bounds(tmp_path, date):
    service, _, path = prepared(tmp_path)
    history(path)
    result = service.get_sessions(ASSET, date=date)
    assert result["week"] == "2025-09-22"
    assert result["requested_date"] == date
    assert result["status"] == "observed"
    assert result["sessions"][0]["open"]["datetime"] == "2025-09-21T22:00:00Z"
    assert result["sessions"][0]["close"]["datetime"] == "2025-09-26T21:00:00Z"
    assert result["metadata"]["history_fallback"] is False
    assert result["metadata"]["coverage"] == "no_known_gaps"
    assert "utc_offset_applied" not in result["metadata"]


def test_daily_observed_includes_historical_sunday_not_only_saved_templates(tmp_path):
    service, _, path = prepared(tmp_path)
    history(path)
    result = service.get_sessions(ASSET, "daily", "2025-09-28")
    assert result["status"] == "observed"
    assert len(result["sessions"]) == 5
    assert result["sessions"][0]["open"]["datetime"] == "2025-09-21T22:00:00Z"
    assert result["sessions"][0]["close"]["datetime"] == "2025-09-22T21:00:00Z"


@pytest.mark.parametrize("mode", ["week", "daily"])
@pytest.mark.parametrize("case,reason", [("empty", "history_unavailable"), ("edge", "incomplete_history"),
                                       ("gap", "incomplete_history"), ("coverage", "coverage_unknown")])
def test_incomplete_history_returns_expectations_and_reason(tmp_path, mode, case, reason):
    service, _, path = prepared(tmp_path)
    if case == "edge":
        history(path, ["2025-09-23T22:00Z", "2025-09-24T00:00Z"])
    elif case == "gap":
        history(path, gaps=[{"after": "2025-09-23T22:00Z", "before": "2025-09-24T01:00Z", "status": "incomplete"}])
    elif case == "coverage":
        history(path)
        (path.parent / "coverage.json").write_text("{}", encoding="utf-8")
    result = service.get_sessions(ASSET, mode, "2025-09-28")
    assert result["status"] == "expected"
    assert result["metadata"]["history_fallback"] is True
    assert result["metadata"]["expectation_reason"] == reason
    assert all(s["basis"] == "expected" for s in result["sessions"])
    if mode == "week":
        assert result["sessions"][0]["open"]["datetime"] == "2025-09-21T22:00:00Z"
    else:
        assert len(result["sessions"]) == 2
        assert result["metadata"]["template_coverage"] == "observed_templates_only"


@pytest.mark.parametrize("date,reason,open_time", [
    ("2026-10-04", "ongoing_week", "2026-09-27T22:00:00Z"),
    ("2026-12-06", "future_week", "2026-11-29T23:00:00Z"),
    ("2025-12-07", "history_unavailable", "2025-11-30T23:00:00Z"),
])
def test_expected_uses_boundary_offset_not_current_summer_offset(tmp_path, date, reason, open_time):
    service, _, _ = prepared(tmp_path)
    result = service.get_sessions(ASSET, date=date)
    assert result["status"] == "expected"
    assert result["metadata"]["expectation_reason"] == reason
    assert result["sessions"][0]["open"]["datetime"] == open_time
    assert result["metadata"]["utc_basis"] == "boundary_dates"


def test_observed_older_schedule_not_clipped_to_current_rule(tmp_path):
    service, calendar, path = prepared(tmp_path)
    history(path)
    calendar["trading_weeks"]["open"] = {"day_offset": 0, "time": "07:00"}
    calendar["trading_weeks"]["close"] = {"day_offset": 4, "time": "12:00"}
    path.write_text(json.dumps(calendar), encoding="utf-8")
    result = service.get_sessions(ASSET, date="2025-09-22")
    assert result["status"] == "observed"
    assert result["sessions"][0]["open"]["datetime"] == "2025-09-21T22:00:00Z"
    assert result["sessions"][0]["close"]["datetime"] == "2025-09-26T21:00:00Z"


def test_observed_without_ready_recurring_pair_and_missing_fallback(tmp_path):
    service, calendar, path = prepared(tmp_path)
    calendar["trading_weeks"]["status"] = "unavailable"
    path.write_text(json.dumps(calendar), encoding="utf-8")
    assert service.get_sessions(ASSET, date="2025-09-22")["reason"] == "week_rule_unavailable"
    history(path)
    assert service.get_sessions(ASSET, date="2025-09-22")["status"] == "observed"


def test_btc_and_moscow_expected_week_ownership(tmp_path):
    service, calendar, path = prepared(tmp_path, "UTC")
    calendar.update(status="no_confirmed_breaks", sessions={})
    calendar["trading_weeks"].update(weekend_belongs_to_next_week=False,
        open={"day_offset": 0, "time": "00:00"}, close={"day_offset": 7, "time": "00:00"})
    path.write_text(json.dumps(calendar), encoding="utf-8")
    result = service.get_sessions(ASSET, date="2025-09-28")
    assert result["sessions"][0]["open"]["datetime"] == "2025-09-22T00:00:00Z"
    assert result["sessions"][0]["close"]["datetime"] == "2025-09-29T00:00:00Z"
    assert service.get_sessions(ASSET, "daily", "2025-09-28")["reason"] == "no_learned_daily_sessions"
    calendar["trading_weeks"].update(timezone="Europe/Moscow", weekend_belongs_to_next_week=True,
        open={"day_offset": -2, "time": "10:00"}, close={"day_offset": 5, "time": "00:00"})
    path.write_text(json.dumps(calendar), encoding="utf-8")
    result = service.get_sessions(ASSET, date="2025-09-28")
    assert result["sessions"][0]["open"]["datetime"] == "2025-09-20T07:00:00Z"


@pytest.mark.parametrize("date", ["2025-9-28", "2025-02-30", "2025-09-28T00:00Z", "", 123])
def test_invalid_dates(tmp_path, date):
    service, _, _ = prepared(tmp_path)
    assert service.get_sessions(ASSET, date=date)["error"]["code"] == "INVALID_PARAMETER"


def test_null_unchanged_and_reads_never_write(tmp_path):
    service, _, path = prepared(tmp_path)
    history(path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert service.get_sessions(ASSET) == service.get_sessions(ASSET, date=None)
    for mode in ("week", "daily"):
        service.get_sessions(ASSET, mode, "2025-09-22")
        service.get_sessions(ASSET, mode, "2025-01-01")
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_expected_dst_transition_boundary_offsets_and_ambiguity(tmp_path):
    service, calendar, path = prepared(tmp_path, "America/Chicago")
    calendar["trading_weeks"].update(weekend_belongs_to_next_week=False,
        open={"day_offset": 0, "time": "00:00"}, close={"day_offset": 7, "time": "00:00"})
    path.write_text(json.dumps(calendar), encoding="utf-8")
    result = service.get_sessions(ASSET, date="2025-11-02")
    assert result["sessions"][0]["local"]["open"]["datetime"].endswith("-05:00")
    assert result["sessions"][0]["local"]["close"]["datetime"].endswith("-06:00")
    calendar["trading_weeks"]["close"] = {"day_offset": 6, "time": "01:30"}
    path.write_text(json.dumps(calendar), encoding="utf-8")
    assert service.get_sessions(ASSET, date="2025-11-02")["reason"] == "ambiguous_or_nonexistent_boundary"


def test_year_boundary_normalization(tmp_path):
    service, _, _ = prepared(tmp_path)
    assert service.get_sessions(ASSET, date="2026-01-01")["week"] == "2025-12-29"


@pytest.mark.parametrize("missing_run", [0, 2, 4])
@pytest.mark.parametrize("mode", ["week", "daily"])
def test_possible_holiday_preserves_observed_short_week(tmp_path, missing_run, mode):
    service, _, path = prepared(tmp_path)
    runs = [pd.date_range(pd.Timestamp("2025-09-21T22:00Z") + pd.Timedelta(days=i),
                          periods=23, freq="h") for i in range(5)]
    previous, following = pd.Timestamp("2025-09-19T20:00Z"), pd.Timestamp("2025-09-28T22:00Z")
    kept = [run for i, run in enumerate(runs) if i != missing_run]
    stamps = [previous] + [t for run in kept for t in run] + [following]
    after = previous if missing_run == 0 else runs[missing_run - 1][-1]
    before = following if missing_run == 4 else runs[missing_run + 1][0]
    history(path, stamps, gaps=[{"after": after.isoformat(), "before": before.isoformat(), "status": "uncertain"}])
    result = service.get_sessions(ASSET, mode, "2025-09-22")
    assert result["status"] == "observed"
    assert result["metadata"]["history_fallback"] is False
    assert result["metadata"]["coverage"] == "uncertain"
    assert result["metadata"]["confirmed_missing_gap_count"] == 0
    assert result["metadata"]["uncertain_1h_gap_count"] == 1
    assert not {"uncertain_gap_count", "gap_timeframe", "gap_count_unit"} & result["metadata"].keys()
    assert pd.Timestamp(result["sessions"][0]["open"]["datetime"]) == kept[0][0]
    assert pd.Timestamp(result["sessions"][-1]["close"]["datetime"]) == kept[-1][-1] + pd.Timedelta(hours=1)
    if mode == "daily":
        assert len(result["sessions"]) == 4
        assert [pd.Timestamp(s["open"]["datetime"]) for s in result["sessions"]] == [run[0] for run in kept]
    assert any("cause is unknown" in note for note in result["notes"])


def test_proven_missing_data_still_falls_back_despite_other_uncertain_gaps(tmp_path):
    service, _, path = prepared(tmp_path)
    history(path, gaps=[
        {"after": "2025-09-22T20:00Z", "before": "2025-09-22T22:00Z", "status": "uncertain"},
        {"after": "2025-09-23T22:00Z", "before": "2025-09-24T01:00Z", "status": "incomplete"}])
    result = service.get_sessions(ASSET, date="2025-09-22")
    assert result["status"] == "expected"
    assert result["metadata"]["history_fallback"] is True
    assert result["metadata"]["coverage"] == "incomplete"
    assert result["metadata"]["confirmed_missing_gap_count"] == 1
    assert result["metadata"]["uncertain_1h_gap_count"] == 1

from dataclasses import replace

import pandas as pd
import pytest

from tv_history.trading_weeks import build_trading_weeks, hourly_trading_week
from tv_history.metadata import compact_calendar, expand_calendar
from test_sync import make_settings


def learned(tmp_path, zone="America/Chicago", start_days=-1, start_hour=17,
            end_days=4, end_hour=16, profile="cme_overnight", holiday=False):
    stamps = []
    for i, monday in enumerate(pd.date_range("2026-02-02", periods=10, freq="7D")):
        start = (monday + pd.Timedelta(days=start_days, hours=start_hour)).tz_localize(zone)
        end = (monday + pd.Timedelta(days=end_days, hours=end_hour)).tz_localize(zone)
        if holiday and i == 4:
            start += pd.Timedelta(days=1)
            end -= pd.Timedelta(days=1)
        stamps.extend(pd.date_range(start, end, freq="1h", inclusive="left").tz_convert("UTC"))
    hourly = pd.DataFrame({"open": 100.}, index=pd.DatetimeIndex(stamps))
    before = hourly.copy()
    cfg = replace(make_settings(tmp_path), timestamp_profiles={"CME_MINI:NQ1!": profile},
                  calendar_timezones={"CME_MINI:NQ1!": zone})
    # Deliberately provide no daily, 4h or weekly data.
    rule = build_trading_weeks("CME_MINI:NQ1!", {"1h": hourly}, cfg, pd.Timestamp("2026-04-08T00:00Z"))
    pd.testing.assert_frame_equal(hourly, before)
    assert rule["status"] == "ready"
    return {"trading_weeks": rule}


def test_future_assignment_dst_and_exclusive_close(tmp_path):
    calendar = learned(tmp_path)
    winter = hourly_trading_week(calendar, "2026-11-08T23:00Z")
    summer = hourly_trading_week(calendar, "2026-06-07T22:00Z")
    assert winter["week"] == "2026-11-09"
    assert winter["start_local"].endswith("-06:00")
    assert summer["week"] == "2026-06-08"
    assert summer["start_local"].endswith("-05:00")
    assert hourly_trading_week(calendar, "2026-06-12T20:00Z") is not None
    assert hourly_trading_week(calendar, "2026-06-12T21:00Z") is None
    assert hourly_trading_week(calendar, "2026-06-07T21:00Z") is None
    with pytest.raises(ValueError):
        hourly_trading_week(calendar, "2026-06-08")


def test_holiday_and_missing_days_do_not_define_new_week(tmp_path):
    calendar = learned(tmp_path, holiday=True)
    assert calendar["trading_weeks"]["matching_weeks"] == 7
    assert calendar["trading_weeks"]["open"] == {"day_offset": -1, "time": "17:00"}
    assert hourly_trading_week(calendar, "2026-09-17T04:00Z")["week"] == "2026-09-14"


def test_moex_saturday_open_and_friday_midnight_end(tmp_path):
    calendar = learned(tmp_path, zone="Europe/Moscow", start_days=-2, start_hour=10,
                       end_days=5, end_hour=0, profile="moex_futures")
    assert hourly_trading_week(calendar, "2026-09-19T07:00Z")["week"] == "2026-09-21"
    assert hourly_trading_week(calendar, "2026-09-18T20:00Z")["week"] == "2026-09-14"
    assert hourly_trading_week(calendar, "2026-09-18T21:00Z") is None


def test_metadata_roundtrip(tmp_path):
    calendar = learned(tmp_path)
    stored = compact_calendar({**calendar, "rules": {}, "session_templates": {}})
    assert expand_calendar(stored)["trading_weeks"] == calendar["trading_weeks"]


def test_insufficient_history_and_old_schema_do_not_guess(tmp_path):
    frame = pd.DataFrame(index=pd.date_range("2026-09-01", periods=3, freq="1h", tz="UTC"))
    cfg = replace(make_settings(tmp_path), calendar_timezones={"ABC:X": "UTC"})
    rule = build_trading_weeks("ABC:X", {"1h": frame}, cfg, pd.Timestamp("2026-09-02T00:00Z"))
    assert rule["status"] == "unavailable"
    assert hourly_trading_week({"trading_weeks": rule}, "2026-09-01T00:00Z") is None
    assert hourly_trading_week({"trading_weeks": {"schema_version": 1}}, "2026-09-01T00:00Z") is None

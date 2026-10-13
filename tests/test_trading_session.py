import asyncio
import json
from datetime import datetime

import pytest

from tv_history.storage import CsvStorage
from tv_history.trading_session import TradingSessionService
from test_sync import make_settings


def setup_calendar(tmp_path, zone="America/Chicago"):
    storage = CsvStorage(make_settings(tmp_path))
    calendar = {
        "schema_version": 2, "status": "ready", "timezone": zone,
        "eligible_sessions": 30, "built_at": "2026-09-24T15:00:00+03:00",
        "trading_weeks": {"schema_version": 2, "status": "ready", "timezone": zone,
                          "weekend_belongs_to_next_week": True,
                          "matching_weeks": 5, "weeks_examined": 12,
                          "open": {"day_offset": -1, "time": "17:00"},
                          "close": {"day_offset": 4, "time": "16:00"}},
        "sessions": {"Thursday": [{"start": "17:00", "end": "16:00", "end_day_offset": 1}],
                     "Monday": [{"start": "17:00", "end": "16:00", "end_day_offset": 1}]}}
    path = storage.path_for("CME_MINI:NQ1!").parent / "calendar.json"
    path.parent.mkdir(parents=True)
    return storage, calendar, path


def call(storage, calendar, path, mode="week", now="2026-09-28T00:00:00+00:00"):
    path.write_text(json.dumps(calendar), encoding="utf-8")
    return TradingSessionService(storage, lambda: datetime.fromisoformat(now)).get_sessions("nq1!:cme_mini", mode)


@pytest.mark.parametrize("now,open_time,close_time,offset", [
    ("2026-09-28T00:00:00+00:00", "22:00:00Z", "21:00:00Z", "-05:00"),
    ("2026-12-01T00:00:00+00:00", "23:00:00Z", "22:00:00Z", "-06:00"),
    ("2026-11-01T06:59:59+00:00", "22:00:00Z", "21:00:00Z", "-05:00"),
    ("2026-11-01T07:00:00+00:00", "23:00:00Z", "22:00:00Z", "-06:00"),
])
def test_week_current_offset_and_evidence(tmp_path, now, open_time, close_time, offset):
    storage, calendar, path = setup_calendar(tmp_path)
    result = call(storage, calendar, path, now=now)
    assert result["asset"] == "CME_MINI:NQ1!"
    assert result["status"] == "recurring_rule"
    assert result["sessions"][0]["open"] == {"day": "Sunday", "time": open_time}
    assert result["sessions"][0]["close"] == {"day": "Friday", "time": close_time}
    assert result["sessions"][0]["local"]["open"]["time"] == "17:00:00"
    assert result["metadata"]["utc_offset_applied"] == offset
    assert result["metadata"]["utc_offset_collected_at_utc"] == now.replace("+00:00", "Z")
    assert result["metadata"]["matching_weeks"] == 5
    assert any("holidays" in note for note in result["notes"])


def test_daily_partial_sorted_overnight_without_inventing_sunday(tmp_path):
    result = call(*setup_calendar(tmp_path), mode="daily")
    assert result["status"] == "partial"
    assert [s["open"]["day"] for s in result["sessions"]] == ["Monday", "Thursday"]
    assert result["sessions"][0]["close"] == {"day": "Tuesday", "time": "21:00:00Z"}
    assert result["metadata"]["built_at_utc"] == "2026-09-24T12:00:00Z"
    assert result["metadata"]["eligible_sessions"] == 30
    assert result["metadata"]["utc_offset_collected_at_utc"] == "2026-09-28T00:00:00Z"
    assert "matching_weeks" not in result["metadata"]


def test_daily_split_and_positive_offset_weekday_wrap(tmp_path):
    storage, calendar, path = setup_calendar(tmp_path, "Asia/Kolkata")
    calendar["sessions"] = {"Monday": [{"start": "01:00", "end": "03:00"},
                                        {"start": "10:00", "end": "12:00"}]}
    result = call(storage, calendar, path, "daily")
    assert len(result["sessions"]) == 2
    assert result["sessions"][1]["open"] == {"day": "Sunday", "time": "19:30:00Z"}
    assert result["sessions"][1]["close"] == {"day": "Sunday", "time": "21:30:00Z"}


def test_continuous_market_week_independent_of_daily_status(tmp_path):
    storage, calendar, path = setup_calendar(tmp_path, "UTC")
    calendar.update(status="no_confirmed_breaks", sessions={})
    calendar["trading_weeks"].update(open={"day_offset": 0, "time": "00:00"},
                                     close={"day_offset": 7, "time": "00:00"})
    result = call(storage, calendar, path)
    assert result["status"] == "recurring_rule"
    assert result["sessions"][0]["open"] == result["sessions"][0]["close"]
    assert any("seven-day" in note for note in result["notes"])
    assert call(storage, calendar, path, "daily")["reason"] == "no_learned_daily_sessions"


def test_moscow_week_preserves_saturday_open_and_friday_utc_close(tmp_path):
    storage, calendar, path = setup_calendar(tmp_path, "Europe/Moscow")
    calendar["trading_weeks"].update(open={"day_offset": -2, "time": "10:00"},
                                     close={"day_offset": 5, "time": "00:00"})
    window = call(storage, calendar, path)["sessions"][0]
    assert window["open"] == {"day": "Saturday", "time": "07:00:00Z"}
    assert window["close"] == {"day": "Friday", "time": "21:00:00Z"}
    assert window["local"]["close"] == {"day": "Saturday", "time": "00:00:00"}


@pytest.mark.parametrize("change,reason", [
    ({"schema_version": 1}, "unsupported_week_rule"),
    ({"status": "unavailable", "reason": "no_recurring_week_boundary"}, "no_recurring_week_boundary"),
    ({"timezone": None}, "no_timezone"),
    ({"timezone": "Invalid/Zone"}, "invalid_timezone"),
    ({"open": {"day_offset": 0, "time": "25:00"}}, "invalid_calendar"),
])
def test_unavailable_week_rules(tmp_path, change, reason):
    storage, calendar, path = setup_calendar(tmp_path)
    calendar["trading_weeks"].update(change)
    result = call(storage, calendar, path)
    assert result["reason"] == reason
    assert result["sessions"] == []


def test_read_only_missing_corrupt_and_invalid_inputs(tmp_path, monkeypatch):
    storage, calendar, path = setup_calendar(tmp_path)
    service = TradingSessionService(storage)
    assert service.get_sessions("CME_MINI:NQ1!")["reason"] == "calendar_unavailable"
    assert service.get_sessions("bad")["error"]["code"] == "INVALID_PARAMETER"
    assert service.get_sessions("CME_MINI:NQ1!", "monthly")["error"]["code"] == "INVALID_PARAMETER"
    path.write_text("{broken", encoding="utf-8")
    assert service.get_sessions("CME_MINI:NQ1!")["reason"] == "invalid_calendar"
    path.write_text(json.dumps(calendar), encoding="utf-8")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setattr(storage, "read", lambda *a: pytest.fail("Must not read price data"))
    service.get_sessions("CME_MINI:NQ1!")
    service.get_sessions("CME_MINI:NQ1!", "daily")
    service.get_sessions("RUS:UNKNOWN")
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_mcp_schema_and_response_without_provider_calls(tmp_path, monkeypatch):
    from tv_history import server
    storage, calendar, path = setup_calendar(tmp_path)
    path.write_text(json.dumps(calendar), encoding="utf-8")
    monkeypatch.setattr(server, "storage", storage)
    async def forbidden(*args):
        pytest.fail("Session lookup must not fetch provider metadata")
    monkeypatch.setattr(server, "add_provider_metadata", forbidden)
    tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "trading_session")
    assert set(tool.inputSchema["properties"]) == {"asset", "session", "date"}
    assert tool.inputSchema["properties"]["date"]["default"] is None
    assert tool.inputSchema["properties"]["session"]["default"] == "week"
    assert tool.inputSchema["properties"]["session"]["enum"] == ["week", "daily"]
    result = asyncio.run(server.trading_session("CME_MINI:NQ1!"))
    assert result.isError is False
    assert result.structuredContent["status"] == "recurring_rule"
    assert json.loads(result.content[0].text) == result.structuredContent
    dated = asyncio.run(server.trading_session("CME_MINI:NQ1!", date="2025-09-28"))
    assert dated.isError is False
    assert dated.structuredContent["week"] == "2025-09-22"
    assert dated.structuredContent["metadata"]["history_fallback"] is True
    assert asyncio.run(server.trading_session("bad")).isError is True

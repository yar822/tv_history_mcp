# Proposed trading_session tool

The [optional-date extension](dated_trading_session_plan.md) documents the
implemented backward-compatible addition. The interface below
describes the current undated behavior.

Implemented 2026-09-28 in `src/tv_history/trading_session.py` and registered in
the MCP server. This document records the agreed interface and construction.

## Interface

```python
trading_session(asset: str, session: Literal["week", "daily"] = "week")
```

- `asset`: required `EXCHANGE:SYMBOL`, with existing legacy format compatibility.
- `session="week"`: recurring weekly opening and closing (default).
- `session="daily"`: available recurring daily session windows during the week.
- No week date, date range, timestamp, timezone, or refresh input.

Return weekday names and clock times in UTC, with exchange-local day/time
alongside them. Opening is inclusive; closing is exclusive, inferred from the
last hourly opening plus one hour. No separate last-bar field is needed.
Weekly windows may contain daily breaks. Weekdays come from the ticker's rule;
do not hardcode Sunday/Friday.

## UTC policy

Convert the saved local patterns using the exchange's UTC offset at request
time, adjusting weekdays across midnight. Apply that same offset to all windows.
Include `utc_basis="current_exchange_offset"` and `utc_offset_applied` in metadata.
`utc_offset_collected_at_utc` records the request-time UTC instant used to
calculate that offset, separate from the calendar's build/training dates.
No specific trading week is selected internally or accepted as input.

This is a current-offset recurring schedule, not an all-year UTC timetable.
UTC times change with daylight saving; callers must request the tool again after
an offset change. It does not predict a dated session spanning a DST transition.

## Construction

Read existing calendar metadata only. No downloads, retraining or cache writes.

Weekly mode reads `calendar.json.trading_weeks`, checking its own schema/status.
The existing learner selects the most frequent hourly first-opening/last-opening
plus one-hour pair, excludes both edge weeks, requires the configured minimum
observations (default four), and rejects ties. It does not apply the daily
learner's agreement threshold. Return matching/examined weeks, training-through
time, build time, source and method; evidence counts are not probabilities.

Daily mode reads `calendar.json.sessions` for a ready session calendar. These
patterns come from recurring contiguous hourly runs separated by physical gaps.
Preserve overnight offsets and separate runs around breaks. Return all available
windows in UTC opening order, using the same window shape as weekly mode.

Daily templates are incomplete for some tickers: NQ has Monday-Thursday local
openings but no Sunday template. Return `status="partial"` and
`coverage="observed_templates_only"`. Missing weekdays mean unknown, not closed.
Do not fill missing days or use weekly bounds to invent daily sessions. Until a
separate completeness check exists, daily template responses remain partial.

Daily construction metadata reports method, eligible training-session count,
training-through time and build time. Per-pattern agreement counts are not saved
in the compact format and must not be invented.

BTC has no learned daily breaks: return daily `unavailable` with reason
`no_learned_daily_sessions`; its weekly rule can still be ready. Missing calendar,
timezone, or supported rules also returns unavailable with a reason and an empty
sessions list. Invalid inputs use existing structured MCP errors.

## Example: weekly

`trading_session("CME_MINI:NQ1!")`, using Chicago's -05:00 offset on the
investigation date. Proposed payload, not a live tool result:

```json
{
  "asset": "CME_MINI:NQ1!",
  "session": "week",
  "status": "recurring_rule",
  "timezone": "UTC",
  "local_timezone": "America/Chicago",
  "sessions": [
    {
      "open": {"day": "Sunday", "time": "22:00:00Z"},
      "close": {"day": "Friday", "time": "21:00:00Z"},
      "local": {
        "open": {"day": "Sunday", "time": "17:00:00"},
        "close": {"day": "Friday", "time": "16:00:00"}
      }
    }
  ],
  "metadata": {
    "source": "cached_1h_timestamps",
    "method": "most_frequent_weekly_boundary_pair",
    "matching_weeks": 11,
    "weeks_examined": 12,
    "built_at_utc": "2026-09-24T12:38:26.447043Z",
    "training_through_utc": "2026-09-24T12:00:00Z",
    "utc_basis": "current_exchange_offset",
    "utc_offset_applied": "-05:00",
    "utc_offset_collected_at_utc": "2026-09-28T00:00:00Z",
    "close_basis": "last_hourly_open_plus_1h"
  },
  "notes": [
    "Expected recurring schedule; holidays and exceptional closures are unknown.",
    "Open is inclusive; close is exclusive. Weekly windows can contain daily breaks.",
    "UTC times use the current exchange offset and change with daylight saving."
  ]
}
```

## Example: daily

`trading_session("CME_MINI:NQ1!", session="daily")` uses the same shape with
`session="daily"`, `status="partial"`, and these available sessions:

| UTC opening | UTC closing | Chicago opening | Chicago closing |
| --- | --- | --- | --- |
| Monday 22:00Z | Tuesday 21:00Z | Monday 17:00 | Tuesday 16:00 |
| Tuesday 22:00Z | Wednesday 21:00Z | Tuesday 17:00 | Wednesday 16:00 |
| Wednesday 22:00Z | Thursday 21:00Z | Wednesday 17:00 | Thursday 16:00 |
| Thursday 22:00Z | Friday 21:00Z | Thursday 17:00 | Friday 16:00 |

Daily metadata uses `method="recurring_contiguous_hourly_runs"`,
`coverage="observed_templates_only"`, and saved `eligible_sessions` (30 for NQ),
instead of weekly agreement counts. Add the note: "Missing weekdays are unknown,
not confirmed closures." These patterns come from the inspected saved calendar.

## Implementation steps

1. Add a read-only service using existing asset parsing and calendar access.
2. Select the saved weekly rule or daily templates based on `session`.
3. Convert boundaries to UTC using the current exchange offset; keep local
   information and overnight relationships. In weekly mode, equal weekday/time
   endpoints mean the following week (BTC Monday to Monday).
4. Format windows, construction metadata, unavailable/partial status and notes.
5. Register `trading_session` with the two parameters and existing MCP response
   conventions; update README/changelog when implemented.
6. Test default/invalid modes, missing rules, partial daily templates, overnight
   and split sessions, weekday wrap, BTC, winter/summer UTC offsets, and no
   provider calls or runtime writes.

## Evidence and validation

The [cache inspection report](../data/study/week_tool_20260928/report.json) covers
nine calendars and hourly CSVs. Eight week rules are ready; VIX lacks a timezone.
RUS:SI1!'s selected pair matches only 5/12 training weeks. COMEX:SI1! and SBER
training evidence ends September 9. Some daily calendars omit weekday templates.
Missing history and exceptional closures are not distinguished by this audit.

Five existing week-rule tests passed during the original investigation.
Implementation validation is recorded in CHANGELOG.md. Production calendars and
price data are read-only inputs to this tool. Restart MCP to expose the new tool.

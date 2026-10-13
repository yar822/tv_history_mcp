# Optional date for trading_session

Implemented 2026-09-28. Incomplete historical evidence falls back to expected
boundaries with an explicit metadata reason, as requested in the final scope.

```python
trading_session(asset, session="week", date=None)
```

## Inputs and week selection

- `date`: optional strict `YYYY-MM-DD`, interpreted as an exchange-local civil
  date. Accept any weekday, not only Monday. No timestamp or date range input.
- Omitted/null date keeps current response and current-offset conversion unchanged.
- Normalize every supplied date to the Monday of its calendar week for every
  asset: `week = date - timedelta(days=date.weekday())`. Monday stays unchanged;
  Tuesday through Sunday move backward to Monday. There is no weekend exception
  for input normalization. `2025-09-28` (Sunday) selects `2025-09-22` for both
  NQ and BTC, replacing the earlier next-Monday input-selection proposal.
- Echo `requested_date` and resolved `week` to make ownership explicit.
- The normalized Monday is the trading-week label. Actual session boundaries
  still follow the asset's schedule, which may open on the preceding weekend.
  For example, the NQ rule for week `2025-09-22` opens Sunday `2025-09-21`.
  Keep existing cached-bar ownership rules: normalizing user input does not
  reassign Sunday evening bars from the following trading week to the prior one.
- `session="week"` returns outer weekly bounds. `session="daily"` returns the
  daily windows within that same selected week, not only the requested day.

## Historical versus expected results

For ended weeks, read cached raw 1h timestamps and group them using the existing
asset-specific trading-week ownership convention, then select the group whose
Monday label equals the normalized request. Use the first observed opening and final observed
opening plus one hour. Do not clip historical observations to today's learned
recurring window: schedules and holiday boundaries may have differed.

Return `status="observed"` for historical boundaries supported by the cache.
These are exact cached bar timestamps; close is still nominal at hourly
resolution, not independently verified exchange closing seconds.

Check that cached groups bracket the requested group and inspect relevant saved
coverage evidence before reporting its bounds. Cache-edge weeks or confirmed
missing-data gaps return `expected` from the saved rule, with `history_fallback=true`
and an explicit `expectation_reason` in metadata. Bracketing and absence of known gaps do not prove complete
exchange history: report coverage as `no_known_gaps` or `uncertain`.

An unexplained gap alone does not prove missing data. Retain observed historical
boundaries when saved gaps are only `uncertain`, so holiday-shaped weeks are not
replaced by normal-week expectations. Metadata reports `uncertain_1h_gap_count`
and `confirmed_missing_gap_count`; `coverage="uncertain"` explicitly leaves the
cause unresolved. Only saved `status="incomplete"` gaps trigger the missing-data
fallback. Missing coverage metadata, truncated caches and absent history retain
their existing fallback behavior. This replaces the initial all-gap fallback.
`uncertain_1h_gap_count` counts gap intervals in hourly history, not missing bars;
no separate timeframe or unit fields are returned.

For an ongoing week, return expected dated opening and closing from the saved
recurring rule with `status="expected"`. Never mistake the latest downloaded
bar for a completed weekly close. Determine ongoing status from request-time
now and the date-specific expected week end; a stale cache alone is not evidence
that a week is ongoing. If observations contradict the expected end and
completion cannot be established, return expectations with a history-fallback reason.

Future weeks also return expected boundaries with reason `future_week`.
An ended week with no cached history returns expectations with
`expectation_reason="history_unavailable"` and `history_fallback=true`.
Unavailable means there is no usable saved rule to construct the expectation.
Never present these predictions as observed history.

Historical grouping requires timezone and week-label metadata but does not
require a ready recurring boundary pair. Expected results do require the
relevant recurring rule. Missing required metadata returns unavailable.

## Daily mode

For historical weeks, split hourly observations into contiguous runs at gaps
greater than one hour, preserving overnight sessions and multiple daily runs.
Each window opens at its first bar and ends one hour after its last bar. Check
saved coverage around gaps so missing data is not silently treated as a confirmed
session break. Confirmed missing-data gaps or truncated caches trigger expected
daily templates with historical-fallback metadata. Uncertain gaps preserve
observed hourly runs with an explicit unknown-cause note; no missing weekdays
or holiday sessions are filled in.
Use the existing learner's definition of a session, not civil-midnight slicing.
BTC with no learned daily breaks retains `no_learned_daily_sessions` rather
than inventing midnight boundaries.

For ongoing/future weeks, instantiate existing daily templates for that week
using their local day offsets and week ownership. Keep missing weekdays unknown;
these have `status="expected"`, `template_coverage="observed_templates_only"`
and `basis="expected"` on the returned windows.
Do not fill missing Sunday templates from the weekly outer bounds.

## Output and DST

Retain the existing sessions list, weekday/time objects and local equivalents.
For dated results, add `datetime` to every opening/closing object: ISO-8601 with
`Z` for UTC and the applicable numeric offset for local time. Add per-window
`basis="observed"` or `"expected"`. Incomplete observations are replaced by
expectations with `history_fallback=true` and an explicit reason in metadata.

Observed timestamps already contain UTC. For expected windows, localize each
boundary at its actual date using the exchange IANA timezone, then convert to
UTC. Do not apply today's offset to a historical/future boundary. Opening and
closing can have different offsets. Ambiguous/nonexistent expected local times
return unavailable instead of guessing.

Dated metadata includes `utc_basis="boundary_dates"`, `evaluated_at_utc`, method,
coverage and evidence timestamps. Local datetimes carry each boundary's offset.
The existing `utc_offset_applied` and `utc_offset_collected_at_utc` retain their
meaning for undated responses; dated responses do not claim that one current
offset was applied to all boundaries.

Historical cache observations can reflect a holiday without identifying it.
Keep a note that no holiday calendar is available and expected boundaries do
not account for holidays or exceptional closures.

## Implementation sequence

1. Add optional `date` to MCP and service signatures with strict validation.
2. Leave the undated branch unchanged. Normalize input dates backward to Monday;
   retain existing asset-specific grouping of cached bars and select that label.
3. Read hourly cache and saved coverage for historical bounds/daily runs, with
   explicit observed, expected-fallback and unavailable results.
4. Reuse weekly boundary resolution and daily templates for ongoing/future
   expectations, converting each boundary with its date-specific offset.
5. Add dated response formatting and evidence metadata; update documentation.
6. Test backward compatibility, all seven input weekdays normalizing to Monday,
   year/month boundaries, unchanged MOEX/NQ/BTC bar ownership, historical schedule
   changes, cache edges/gaps/staleness, missing history, holiday-shaped data,
   ongoing/future results, split/overnight daily sessions, winter/summer DST and
   transition boundaries. Verify no downloads or runtime writes.
7. Validate against local/copied caches, then live MCP after implementation and
   restart; record these validation types separately in the changelog.

The tool remains read-only and does not initialize or refresh history.

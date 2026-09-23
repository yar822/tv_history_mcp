# Changelog

Meaningful changes to tv-history MCP, newest first. Entries describe implemented
behavior; dates are work dates rather than published release versions.

## 2026-09-17

### Omit internal metadata from MCP responses

- All three tools now omit `timestamp_normalization`,
  `daily_timestamp_normalization`, `history_coverage`, and
  `daily_history_coverage` from text and structured responses, including nested
  error details. Previously these blocks exposed large timestamp and gap lists.
- Filtering occurs at serialization without mutating internal metadata. Coverage
  checks, repairs, per-bar provenance, error codes/messages, and chart images
  remain unchanged. Updated README to reflect the public response contract.
- Validation: 113 focused automated tests passed across response serialization,
  bars, charts, analysis, coverage, timestamp profiles, and provider metadata.
  No cache-copy or live MCP validation was performed. Restart MCP to activate.

### Stop constructing unused diagnostics during tool requests

- Removed timestamp summary lists/counts and the profile verification passes
  that only populated those summaries. Previously the response filter hid them
  after construction. Daily/weekly label mapping, OHLCV, source timestamps,
  evidence, and ambiguity flags remain intact.
- Services no longer attach discarded metadata. Tool calls skip the optional
  synchronizer coverage report, and charts skip their output-only coverage
  calculation. Repair eligibility, analysis coverage gates, and bars' missing-data
  checks remain active; internal callers can still request coverage reports.
- Validation: the initial full suite passed 276 tests and found four obsolete
  metadata assertions, which were updated. The focused rerun passed 142 tests;
  two new repair cases then passed after correcting a frequency-only assertion
  for CSV-loaded indexes. Tests cover preserved provenance and repair behavior,
  plus skipping redundant coverage work. No cache-copy/live MCP validation or
  latency benchmark was performed. Restart MCP to activate.

## 2026-09-15

### Saved provider delay metadata

- Added a separate symbol-only lookup at startup for known tickers and before
  the first MCP response for new tickers. Saved once per ticker per process in
  existing `coverage.json`; analysis, bars, and chart responses expose
  `provider_metadata` with resolved symbol, delay seconds, and token auth status.
- Existing OHLCV extraction is unchanged. Metadata lookups use the provider lock;
  concurrent callers reuse a ticker-specific saved result. Failures produce
  unknown metadata; absent delay is null rather than an assumed zero.
- Live metadata-only validation with the configured token returned RUS:SI1!
  with null delay and COMEX_DL:GC1! with 600 seconds, both authenticated.
  Invalid-token protocol probing confirmed explicit rejection. No live MCP
  restart or production history refresh was performed.
- Validation: 44 focused tests passed, covering metadata isolation, persistence,
  startup, auth/failure cases, all three MCP response types, history coverage,
  and refresh reuse. An initial full-suite run passed 270 tests and caught four
  startup-check regressions from a misplaced return; corrected and verified by
  the focused rerun. Restart MCP to activate.

### Load TradingView authentication token from the project root

- The provider now reads `_token.txt` when creating its client and applies a
  non-empty token before fetching bars. Previously only username/password
  environment variables were used. Missing or empty files retain that fallback.
  The path is independent of the working directory; token contents are not logged.
- Validation: [token-loading check](study/check_token_loading.py) verified loading
  from another working directory, client reuse, and missing/empty-file fallback.
  A direct provider-client request at 14:34:47 UTC+3 returned the current 14:34
  RUS:SI1! one-minute candle. No live MCP process was restarted or MCP cache changed.
- Restart MCP to load the change or a replacement token. Real-time availability
  still depends on the supplied token's access to each exchange.

## 2026-09-10

### Ten-second reuse for simultaneous monitor requests

- Successful fetches now suppress redundant ordinary refreshes for 10 seconds
  per canonical asset/timeframe, checked under the synchronizer lock. Multiple
  monitors reuse stored prices while retaining individual cutoff/window and
  completion evaluation. Original receipt evidence is preserved.
- The in-memory monotonic timer starts only after a successful fetch is stored.
  Failures clear it; explicit coverage repairs and disjoint fallback bypass it.
  Forming prices can be up to 10 seconds older than a fresh provider response.
- Restart MCP to activate. The full run passed 266 tests and exposed two test
  fixture/expectation issues: the new response test needed indicator settings,
  and the existing overlap-refresh test needed to advance beyond the reuse window.
  After those test-only corrections, all 15 reuse/synchronizer tests passed,
  covering the two failures. No live server was restarted during implementation.

### Investigated lagging RUS hourly output

- Live MCP/direct-source checks found no UTC conversion mismatch for GC/MX/SI.
  MX/SI downloads through 06:13 UTC lacked the 06:00 successor; gold had it at
  06:11. Agent retries exhausted at 06:13 and scheduled the next check for 07:03.
  Checks at 06:22 returned completed 05:00 bars for all three assets. No finality
  or timezone code changed; live requests refreshed the caches normally.
- Evidence and recommended polling adjustment are in the
  [timestamp investigation](data/live_checks/20260910_062230_hourly_timestamp/report.md).

### Investigation code moved to study

- Moved 38 investigation/validation Python source files, including the frozen
  comparison baseline, from `data/` into matching paths under `study/`.
- Updated tool references and preserved existing data/report output locations.
  AGENTS.md now requires ad hoc tool code and source baselines under `study/`.
- Checked Python syntax and confirmed no executable source files remain in data.
  This organization-only change requires no MCP restart.

### Smaller coverage state and shared session calendar

- Calendar schema 2 stores readable weekday session ranges once for all four
  timeframes. Verified native bar alignment references session endings; reopening
  is derived from the shared schedule unless an explicit provider override is
  necessary. Runtime lookup preserves verified boundaries, unknown alignments,
  DST behavior, date exceptions, suspensions and public completion fields.
- Coverage stores each unresolved gap once with its repair-attempt flag, removes
  the duplicate startup result snapshot, and drops resolved repair markers when
  persisting state. The latest startup signatures remain for unchanged-cache reuse.
- Receipt pruning removes evidence only when the successor is at or before the
  calendar effective date: earlier cutoffs cannot use that calendar, and later
  cutoffs already have the successor. Remaining evidence keeps request start and
  OHLCV fingerprint. This supersedes the earlier retain-every-receipt policy;
  latest-only pruning is deliberately avoided when historical evidence can matter.
- Migration runs automatically on restart after the normal backup. Calendar
  version/effective time are preserved for unchanged inputs. Revision logs and
  price CSV formats are unchanged.
- Full suite: **265 tests passed**. Copied-cache validation preserved all 32 CSVs,
  all verified boundary/resumption offsets and 672 completion comparisons.
  MX coverage shrank from 5,333,192 to 421,315 bytes and its calendar from 11,169
  to 3,408 bytes. All eight copied tickers retained four necessary receipts each.
  See the [validation report](data/live_checks/20260909_213742_metadata_simplification/report.md).
  Subsequent live validation after the user restarted MCP confirmed schema 2
  across all eight tickers, 32 cached requests without downloads (0.064–1.420
  seconds each), and four MX refreshes with valid completion evidence. Another
  startup on a migrated cache copy took 1.034 seconds including backup, with no
  price reads, provider requests or metadata rewrites. See the
  [live restart report](data/live_checks/20260909_214256_simplified_live/report.md).

### Consolidated revision logs

- Store new OHLCV revision records in one `revisions.jsonl` per ticker, retaining
  the timeframe in each record. Updates to the previously latest cached bar,
  including its closing refresh when a successor arrives, are no longer logged.
  Prices still update normally; later changes to older bars remain recorded.
- At startup or on the next merge, consolidate legacy timeframe revision logs atomically before
  removing them. Existing records are preserved; duplicate records from an
  interrupted migration are not appended twice.

### Receipt evidence consolidated into coverage metadata

- Replaced per-timeframe receipt files with `coverage.json.receipt_evidence`,
  grouped by timeframe and raw timestamp. Preserved all historical fetch times
  and OHLCV fingerprints; completion rules and historical-cutoff behavior remain
  unchanged. Public response fields do not expose the receipt maps.
- Startup migrates legacy receipt files after the usual backup, including when
  cached prices/calendars are unchanged. Storage merges also handle migration.
  Old files are removed only after successful atomic persistence; retries retain
  newer consolidated records if both formats exist.
- Coverage writes and receipt updates share the storage lock so diagnostic saves
  cannot overwrite new evidence or another timeframe's receipts. A bounded cache
  of 16 parsed coverage documents uses file size/modification time for invalidation;
  returned data is isolated from the internal cached maps.
- Copied-cache validation preserved 131,969 receipt records across eight tickers,
  all 32 price CSVs byte-for-byte, and completion results at live/historical cutoffs.
  Legacy receipt/revision files were removed from the migrated copy. Mean cached
  storage-read time was 0.043 seconds. Targeted migration, revision, calendar and
  coverage tests passed (69 tests).
- Final full suite: **261 tests passed** in 87.61 seconds. See the
  [migration validation report](data/live_checks/20260909_211343_receipt_migration/report.md).
- Live validation after the user restarted MCP confirmed migration of all 131,969
  receipt records and unchanged price CSVs against the startup backup. Legacy
  files remained absent after provider refreshes; completion evidence and public
  response checks passed. All 32 fully cached HTTP requests returned without
  downloads in 0.122–1.751 seconds each. The initial historical weekly requests
  triggered refreshes under the existing cutoff rule; see the
  [live validation report](data/live_checks/20260909_211949_consolidation_live/report.md).

## 2026-09-09

This entry consolidates the MCP changes made during the investigation of changing
bar prices between agent-loop runs, missing NQ history, and slow MCP startup.

### Daily/weekly trading-date correction (September 7–9 work)

- **Problem:** TradingView raw timestamps describe session openings rather than
  the trading-date labels shown on its chart. MX/SI Monday bars could open on
  Saturday or an earlier evening; other futures also open the preceding evening.
  Treating those timestamps as calendar-day/week labels and adding 24 hours or
  seven days admitted episode-week prices into pre-week context. For MX on
  September 7, different snapshots of the same live daily bar changed startup
  classification from `SIDE_VOLATILE/SWINGS` to `UP/CONTINUATION`.
- **Fix:** Normalize served 1D bars to their trading date at `00:00 UTC`, and 1W
  bars to the Monday of the opening daily bar's corrected trading week. Preserve
  raw CSV timestamps and OHLCV; expose the original as `source_timestamp`, with
  timestamp basis, evidence and ambiguity metadata. This transformation does not
  resample prices or modify 1h/4h timestamps. The consumer `run.py`, agent-loop and
  engine adapter required no timestamp-repair changes.
- **Standard fallback:** Evaluate openings in the configured exchange timezone,
  including DST. Evening ranges are local `[18:00, 24:00)` for MOEX,
  `[16:00, 24:00)` for CME/COMEX, and `[22:00, 24:00)` for Brent. Evening openings
  advance to the next weekday; MOEX's observed `06:00 UTC` additional-session
  template also advances to the next weekday. Other daytime openings retain the
  local date; Bitcoin retains its UTC calendar date. Ranges replaced fixed-hour
  recognition, including historical GC Sunday 16:00 Chicago openings that had
  otherwise been assigned to the previous week.
- **Intraday correction:** Cached 4h evidence can correct the fallback when it
  brackets successive daily openings and matches daily open/high/low. A shortened
  final 4h bar requires a matching contiguous hourly prefix. Holiday handling
  checks the final contiguous session after a full-day gap, using exchange-specific
  shapes: MOEX daytime sessions require matching volume; CME overnight and Brent
  daytime/overnight sessions permit separately revised volume and report that
  difference. This replaced the MOEX-only shape/volume checks previously applied
  to other exchanges. Weekly ownership follows the corrected opening daily bar.
- **Verified examples:** MX/SI `2026-09-05T06:00Z` maps to September 7;
  `2025-12-30T16:00Z` maps to January 5, 2026. NQ/GC `2026-06-18T22:00Z` maps to
  June 22 for both daily and weekly bars. The earlier NQ fallback labelled these
  June 19 and June 15 respectively, exposing Monday's daily high and the episode
  week's weekly low to the June 22 run. Corrected labels exclude those bars at
  the Monday opening cutoff through the consumers' existing completed-session
  filters.
- **Validation:** September 9 timestamp-specific validation passed 131 targeted
  automated tests and 22 live 1D/1W requests across MX, SI, GC, NQ, Brent and BTC.
  Actual `run.py` filters excluded all 22 target bars at their Monday cutoffs and
  retained preceding context. This was a targeted regression check, not a repeat
  of the earlier 59-episode audit or a claim that all historical leaks are solved.
- **Limits:** Date ownership remains calendar-free, separate from the completion
  calendars described below. Missing or inconclusive intraday evidence retains
  the operator-requested standard fallback; it does not omit the bar. That date
  can occasionally be a holiday. Duplicate labels are preserved and flagged,
  never silently merged. Later cached evidence can refine ownership, and source
  price revisions remain possible; this is not a point-in-time data archive.

### Bar completion and trading calendars

- Replaced ordinary latest-bar completion based on elapsed time with successor
  evidence: a later raw bar in the same asset/timeframe confirms the preceding
  bar. Normalized daily/weekly labels do not determine successor ordering.
- Restricted delayed completion without a successor to verified physical
  session/period boundaries. It requires a download started after the boundary
  plus the asset's delay, containing the target bar. Stored receipt times and an
  OHLCV fingerprint prevent stale evidence from authenticating changed prices.
- Kept asset-specific delays, with asset overrides taking precedence over exchange
  defaults and the global default. The same configured delay applies to 1h, 4h,
  1D and 1W boundary exceptions. RUS examples use five minutes; configured COMEX,
  CME_MINI and ICEEUR defaults use fifteen minutes.
- Added per-ticker calendars learned from 30 qualifying completed trading-session
  dates within a 90-day lookback. Weekday/session patterns require at least four
  observations and 90% agreement. Partial cache-edge sessions, anomalous shapes
  and missing session segments are excluded rather than learned as closures.
- Added native-timeframe boundary checks, date-specific close overrides, and
  runtime suspension of rules contradicted by received hourly bars. Ordinary
  requests do not continuously relearn the calendar. Calendar reuse/rebuilding at
  launch follows the startup metadata policy below.
- Added `completion_reason`, `completion_boundary`, and `completion_calendar`
  response metadata. Bars, analysis and charts share completion evaluation.
  Existing fields and their types are retained; consumers that reject unknown
  JSON keys need to allow the added fields.

### Exchange rules and new tickers

- Unified calendar timezones and timestamp profiles in
  `market_rules.exchanges`, with exchange defaults and per-symbol overrides.
  An omitted symbol field inherits its exchange setting; an explicit
  `timestamp_profile: null` preserves native timestamp labels.
- Configured RUS as `Europe/Moscow` / `moex_futures`, with null profile overrides
  for SBER and YDEX; COMEX and CME_MINI as `America/Chicago` / `cme_overnight`;
  ICEEUR as `Europe/London` / `ice_brent`; and BITSTAMP as `UTC` / `utc_calendar`.
  These are application configuration rules, not timezone metadata supplied by
  tvDatafeed. Provider naive-timestamp parsing remains a separate setting.
- Used the resolved timezone for both profiles and calendar boundaries, including
  local civil-time calculations across DST. Legacy configuration is supported
  when unified rules are absent; mixing both formats is rejected.
- New tickers initialize all four native timeframes (1h, 4h, 1D, 1W) before
  responding, using the configured history limit, currently 5,000. Retained retry
  caps of 5,000/5,000/5,000/4,000/2,000 bounded by that limit. Successful timeframe
  initialization persists so later requests retry only missing timeframes.
- Canonical ticker registration prevents duplicate caches for aliases. Both
  `EXCHANGE:SYMBOL` and `SYMBOL:EXCHANGE` remain accepted; response asset strings
  preserve the requested ordering, uppercased. SBER inherits the RUS timezone
  while retaining its native daily/weekly labels.

### History continuity, repairs and diagnostics

- Corrected incremental request sizing to use actual download time rather than
  the historical query cutoff: tvDatafeed returns recent history and does not
  receive that cutoff. Requests include an overlap, currently five bars, and
  escalate to a full batch when a small response does not overlap the cache.
- Added requested-window coverage checks for count, session and date windows.
  Being earlier than the cache maximum no longer establishes complete coverage.
  Unexplained gaps or insufficient historical reach can trigger a full-batch
  repair for the affected ticker/timeframe.
- Added persisted gap/repair records and download range/overlap diagnostics.
  Repeated requests and restarts do not repeatedly download for the same recorded
  unsuccessful repair. Valid data is merged by raw timestamp, with refreshed
  values replacing overlaps and OHLCV revisions recorded separately.
- Added `history_coverage` with `no_known_gaps`, `uncertain`, or `incomplete`.
  Confirmed missing/unavailable analysis history returns
  `INSUFFICIENT_HISTORY_COVERAGE`. Exact learned resumptions can explain closures;
  crossing a weekend alone cannot. Daily/weekly date labels and holiday tails do
  not independently prove missing intraday candles. No synthetic bars are inserted.
- Recovered 171 missing NQ hourly bars in the previously identified August gap.
  Subsequent live checks confirmed all recovered timestamps remained present and
  the August 24 requested window returned the correct bars.

### Startup safety and metadata reuse

- Added a startup ZIP snapshot before history mutations. Removed history download
  side effects from importing the server module. Added an early HTTP address check
  so an occupied port is rejected before backups and history refreshes begin.
- Replaced the initial implementation's unconditional full startup refresh
  (eight assets times four timeframes: 32 downloads) with selective checking.
  This supersedes the full-refresh startup policy described in earlier reports.
- Added `startup_check` metadata in each ticker's `coverage.json`: four CSV file
  sizes/modification times, relevant settings/checker version, saved calendar file
  signature, checked range, and result. Unchanged inputs reuse both the saved
  calendar and gap-check result without scanning price files or downloading data.
- Changed inputs or missing metadata trigger calendar rebuilding and a local check
  of 100 calendar days ending at the ticker's latest cached timestamp. A preceding
  bar is retained to detect gaps crossing the window's start. Staleness relative
  to wall-clock time alone does not trigger a startup download.
- Startup repairs only affected timeframes with newly detected, unattempted gaps.
  A shorter intraday tail can be repaired when another cached intraday timeframe
  proves trading continued. Repairs are merged and rechecked; failures retain
  cached data, errors and attempt records while other affected timeframes continue.
- Historical requests outside the startup window still receive a full local audit.
  Ordinary downloads invalidate startup file signatures for the next launch but
  do not rebuild calendars during the request. Current completion and fresh-receipt
  checks remain evaluated per response, not cached as startup results.

### Performance and validation

- Removed repeated copying of large pandas receipt/calendar metadata during row
  iteration, storage comparisons, normalization, indicator and structure work.
  The recorded live NQ analysis case improved from 104.88 seconds to 4.56 seconds;
  these are observed runs, not a guaranteed latency bound.
- Final automated suite after the startup metadata change: **246 tests passed**
  in 85.51 seconds, including unchanged-cache reuse, changed-input invalidation,
  selective repair, failed repair persistence, boundary-crossing gaps, proven
  missing tails, and historical checks outside the startup window.
- On a copy of the eight-asset cache, the initial local check/calendar build took
  **11.799 seconds** and an unchanged restart took **1.151 seconds**, including
  backup work. Neither run made a provider request.
- Final live validation: **56 checks passed** across eight assets and all four
  timeframes, including analysis, charts, SBER aliases, recovered NQ history and
  unavailable-history behavior. The latest launch reused all eight calendars and
  metadata records with **zero startup downloads**.
- Those live requests caused 59 incremental downloads of six or seven bars and
  one 5,000-bar attempt for the deliberate year-2000 NQ request. Repeating the
  unavailable-history request caused no further download. Calendars retained their
  versions during the calls; latest bars without successors stayed incomplete.

### Cached-request speed optimization

- Removed repeated normalized-view construction on cache hits. Coverage-only
  changes update the attached diagnostics without normalizing prices again;
  repairs still rebuild the view with current prices, receipts and calendar state.
- Weekly profiles now pass their computed daily mapping into weekly normalization,
  reducing full daily-normalization passes from four to one per cached weekly
  request. Tuple iteration and indexed slices replace repeated pandas row/boolean
  allocations while retaining the complete evidence and timestamp diagnostics.
- Raw-bar responses evaluate completion for selected output rows while retaining
  the complete raw successor and receipt context. Analysis still evaluates its
  required full history. Download and mutation locking remains in place.
- All 39 frozen-cache before/after responses matched exactly across eight assets,
  including 36 bar requests, two analyses and one chart (including image hash).
  NQ count-100 local service times changed from 4.191 to 1.201 seconds for 1D,
  and from 7.138 to 1.332 seconds for 1W. Neither version downloaded data.
- Thirty optimized HTTP requests on a temporary MCP with copied data also matched
  the frozen baseline exactly and made no provider calls. A concurrent daily/weekly
  pair took 2.514 seconds and 22 alternating requests took 28.326 seconds, compared
  with the earlier live measurements of 10.763 and 121.564 seconds. The temporary
  test server was shut down; the user's MCP was not restarted by this task.
- Final full suite: **254 tests passed** in 87.08 seconds, including repaired-view
  invalidation and selected-bar successor/boundary-receipt regression tests.
- These are cached-path measurements, not tvDatafeed latency guarantees. No
  persistent response cache, history truncation or weaker finality/gap checks were
  introduced. Restart MCP to load the changed code.

### Documentation and remaining limits

- Investigated cached-request latency without changing production request logic.
  For NQ `asset_bars` at `2026-09-04T12:00:00Z`, count 100, two sequential runs
  measured 0.770/0.785 seconds (1h), 4.028/4.157 seconds (1D), and 7.107/6.734
  seconds (1W). A concurrent daily/weekly pair took 10.763 seconds; 22 alternating
  daily/weekly requests took 121.564 seconds. The download log remained unchanged.
  Local cache-copy profiling identified duplicate normalized-view construction,
  four full daily-normalization passes per weekly request, completion evaluation
  across all opened history, and the synchronizer-wide lock around cached work.
  No request-time gap audit or calendar rebuild occurred in the profiled calls;
  coverage metadata writes took about three milliseconds. These measurements do
  not establish an exact regression against earlier batches with unspecified
  inputs. The subsequent optimization above removes repeated processing; the
  synchronizer-wide lock remains. See the investigation and optimization reports.
- Added this changelog and the requirement in [AGENTS.md](AGENTS.md) to maintain it
  alongside future meaningful MCP changes. Added a changelog link to the README.
- Later provider/settlement revisions can still change historical prices;
  completion evidence does not make TradingView history immutable. Existing
  agent-loop decisions are not rewritten, and agent-loop source was not changed.
- Some historical intervals and current tails remain explicitly uncertain.
  Provider history limits can leave gaps unrecoverable. Calendars with insufficient
  evidence cannot authorize learned boundary exceptions; hourly data cannot infer
  exact sub-hour closures.
- The final live run did not cross an actual session/day/week closing boundary;
  delayed-boundary behavior was covered by automated tests. First-time ticker
  initialization was covered by automated tests; the final live run used existing
  tickers. Validation results describe the observed runs, not perpetual server state.

Supporting local evidence (generated data artifacts may be absent from a fresh
checkout):

- [Timestamp restart validation, 22 live responses](../../202607_algo_llm/agent-judge-release-h4/agent_calibrated_v19/research_execution/mcp_timestamp_restart_20260909.json)
- [Earlier exchange holiday validation](../../202607_algo_llm/agent-judge-release-h4/agent_calibrated_v19/research_execution/exchange_holiday_profiles_live_20260908.json)
- [Initial repair and performance report](data/live_checks/coverage_repair_20260909/report.md)
- [Earlier restart validation](data/live_checks/20260909_192321_restart_validation/report.md)
- [Cache-copy startup timings](data/live_checks/startup_metadata_20260909_195822/result.json)
- [Final live metadata/startup validation](data/live_checks/20260909_200449_metadata_live_validation/report.md)
- [Cached-request latency investigation](data/live_checks/20260909_201841_cached_latency/report.md)
- [Cached-response optimization and equivalence checks](data/live_checks/20260909_202640_speed_optimization/report.md)

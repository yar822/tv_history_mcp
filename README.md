# tv-history MCP

Standalone historical asset-analysis MCP server backed by tvDatafeed and local
timeframe-specific CSV files.

See [CHANGELOG.md](CHANGELOG.md) for dated changes and validation results.

Assets are loaded dynamically. TradingView-style `EXCHANGE:SYMBOL` is preferred,
for example `RUS:MX1!`. The existing `SYMBOL:EXCHANGE` format remains accepted
for backward compatibility. A new ticker request initializes all four native
timeframes (1h, 4h, 1D, 1W) before answering, independent of response count.

## Install and run

### Provider metadata

Analysis, bars, and chart MCP responses include `provider_metadata`, for example:

```json
{"provider_metadata": {"resolved_symbol": "COMEX_DL:GC1!", "delay_seconds": 600, "auth_token": true}}
```

A separate symbol-only lookup runs once per known ticker at startup, and once
before the first response for a new ticker. The result is stored in the ticker's
existing `coverage.json` and reused until restart. It does not change OHLCV
extraction or completion rules. Lookups are serialized with provider downloads.
`delay_seconds: null` means no numeric delay was reported. `auth_token` is true
when symbol resolution succeeds after token authentication, false for anonymous
access or explicit rejection, and null when status cannot be established.
Lookup failures return unknown metadata without preventing normal bar requests.
Each unavailable ticker can add up to the lookup timeout (5 seconds, plus connection
cleanup) to startup. Restart to refresh metadata after changing the token.

```powershell
cd C:\_tools\202607_tradingview_mcp\tv_history
uv sync
uv run tv-history-mcp
```

HTTP transport:

```powershell
uv run tv-history-mcp streamable-http --host 127.0.0.1 --port 8010

#or for Tailscale IP
uv run tv-history-mcp streamable-http --host 0.0.0.0 --port 8010 
```

The MCP endpoint is `http://127.0.0.1:8010/mcp`.

Codex registration for the HTTP server:

```powershell
codex mcp add tv-history --url http://127.0.0.1:8010/mcp
```

Or let Codex launch the stdio server:

```powershell
codex mcp add tv-history -- C:\_tools\202607_tradingview_mcp\tv_history\.venv\Scripts\python.exe -m tv_history.server
```

## Tool

```text
asset_analysis(
  asset="BITSTAMP:BTCUSD",
  timeframe="1h",
  timestamp="2026-07-01T12:00:00Z"
)
```

When `timestamp` is omitted, `price_data` may contain the latest received
non-final bar. Its `is_bar_complete` status uses successor confirmation or a
verified session-boundary exception with fresh data;
all derived analysis continues to use finalized bars only.

The compact execution response is the only response format and is returned by
default. The `response_version` parameter may be omitted. Use
`include_indicators=true` when raw indicators are needed.

```text
asset_analysis(
  asset="BITSTAMP:BTCUSD",
  timeframe="4h",
  timestamp="2026-07-01T12:00:00Z",
  include_indicators=true
)
```

```text
asset_bars(
  asset="ICEEUR:BRN1!",
  timeframe="1h",
  timestamp="2026-03-23T00:00:00Z",
  sessions=4
)
```

`asset_bars` returns OHLCV bars oldest-first. It includes an opened latest bar by
default and marks each bar with `is_bar_complete`. Use `count` (1–1000) or `sessions`;
when both are supplied, `sessions` wins.

```text
asset_chart(
  asset="BITSTAMP:BTCUSD",
  timeframe="4h",
  timestamp="2026-07-01T12:00:00Z",
  days="10"
)
```

`asset_chart` returns a PNG candlestick chart with red/green bodies, wicks, and
volume, plus a compact metadata block describing the asset and chart range.
Use `sessions` instead of `days` to request a fixed number of trading dates;
`sessions` wins when both are supplied. All tool errors use the same structured
error object and are marked as MCP errors.

Supported timeframes are `1h`, `4h`, `1D`, and `1W`. Each timeframe is fetched
directly from tvDatafeed and stored independently as `1h.csv`, `4h.csv`,
`1D.csv`, or `1W.csv`. Analysis and charts read the requested timeframe file;
they do not construct higher-timeframe bars from `1h.csv`.

### Completion policy and frozen calendars (2026-09-09)

Restart MCP to activate these changes. Running agent-loop processes retain bars
they already consumed; this update does not rewrite earlier trading decisions.

Live validation on September 9 identified repeated pandas metadata copies during
row iteration, especially with large BTC history. The follow-up fix keeps receipt
and calendar evidence outside per-row iteration and normalization inputs. Restart
MCP again if it was launched before that fix. The completion policy is unchanged.

Cached responses reuse the normalized price view when coverage checking has not
changed source data. Weekly profiles share the already computed daily mapping;
raw-bar responses evaluate completion only for returned rows while retaining full
raw successor and receipt evidence. Normalization still examines the supporting
history and returns its existing diagnostics. Data repairs rebuild the view.
Download/mutation locking is retained. See the changelog for before/after response
comparisons and cached-request timing results; restart MCP to load the optimization.

Ordinary latest bars remain incomplete regardless of elapsed time. A later raw
source bar in the same asset/timeframe series confirms the preceding bar. Raw
source timestamps determine ordering even when 1D/1W labels are normalized.

Without a successor, completion requires a confidently learned physical boundary
and a successful download **started after boundary + asset delay**, containing
the target candle. Receipt metadata includes an OHLCV fingerprint so stale receipt
evidence cannot authenticate a changed CSV row. Default delay is five minutes;
asset overrides take precedence over exchange overrides and the default. The
same asset delay applies to 1h/4h/1D/1W. It is not applied to ordinary intraday bars.
These delays do not guarantee immunity from later provider/settlement corrections.

Startup first backs up the caches, then checks per-ticker metadata in
`coverage.json`. If the four CSV file sizes/modification times, relevant settings,
checker version and saved calendar file are unchanged, it reuses the calendar and
gap-check result without reading price files or downloading history. Missing
metadata or changed inputs trigger calendar rebuilding and a local gap check of
the last 100 calendar days ending at the ticker's latest cached timestamp. One
preceding bar is retained to detect holes crossing the check window's boundary.
An old cache alone does not trigger a startup download. A shorter intraday tail
can trigger repair if another cached intraday timeframe proves trading continued;
the check never treats the interval from the cache end to now as a missing range.

Only timeframes with unexplained gaps not already attempted receive a full-batch
repair. After merging, startup rechecks coverage and rebuilds the calendar if data
changed. Failed repairs retain cached data and error/attempt records; other
affected timeframes continue. Unresolved intervals remain explicit, and a restart
alone does not repeat the same unsuccessful repair. Metadata is saved after the
check and any repairs. Historical requests older than the startup window still
receive a full local coverage audit in the normal request path.

New tickers build calendars after completing four-timeframe initialization.
Ordinary downloads do not rebuild calendars; changed files invalidate their
startup metadata for the next launch. Runtime hourly observations contradicting
a predicted break suspend the affected rule without learning a replacement.
Completion flags still evaluate current cutoff and fresh receipt evidence per
request; they are not cached in startup metadata. Importing the server module
does not start downloads. Direct synchronizer users opt into the startup check
and selective repair lifecycle with `refresh_on_start=True` (the MCP entry point
does so).

For HTTP transport, the entry point checks the configured listen address before
starting backups or history downloads. If another process owns the port, it exits
with a clear error and does not refresh the caches. Stop the old MCP instance
before launching its replacement on the same port.

The learner selects 30 qualifying trading-session dates within a 90-day bound.
Contiguous hourly segments are grouped by their local starting date, so multiple
segments around an intraday break count once. Overnight segments retain their
starting date. The first/last partial runs, anomalous segment shapes, and dates
missing expected segments are excluded. Recent weekday/start-time templates need
at least four observations and 90% agreement; older matching dates may fill the
30-date window. Unknown timezones or insufficient data disable learned exceptions.
`market_rules.exchanges` is the shared source for calendar timezones and daily/
weekly timestamp profiles. Each ticker inherits its exchange's `timezone` and
`timestamp_profile`; entries under `symbols` override individual fields. An
omitted field inherits its default, while `timestamp_profile: null` explicitly
preserves native timestamp labels. Symbol names are case-insensitive bare ticker
names, and requests accept both EXCHANGE:SYMBOL and SYMBOL:EXCHANGE.

RUS defaults to Europe/Moscow and moex_futures, with explicit null profile
exceptions for SBER and YDEX. COMEX/CME_MINI default to America/Chicago and
cme_overnight, ICEEUR to Europe/London and ice_brent, and BITSTAMP to UTC and
utc_calendar. This intentionally applies the configured profile to new tickers;
add symbol exceptions for instruments using different date conventions. Session
hours and breaks are still learned separately per ticker. Unconfigured exchanges
retain native labels and have no learned boundary exceptions without a timezone.

Restart MCP after changes to rebuild existing calendars. Local civil times,
rather than fixed UTC offsets, instantiate boundaries across DST. Profiles now
receive the same resolved timezone used by the calendar. Legacy profile/timezone
configuration remains supported when `market_rules` is absent; mixing both
formats is rejected to avoid ambiguous precedence. Legacy defaults in Python
exist only for compatibility, not as a timezone source for unified market rules.

1h exceptions apply before physical breaks; 4h exceptions require the native
candle to end there (including verified shortened final candles). 1D/1W exceptions
require the final segment of the native daily/weekly period. Native O/H/L must
match the corresponding hourly history; settlement close and volume are not used
to infer ownership. There must be sufficient repeated evidence for **each** rule.
A ready calendar may therefore have no verified weekly rule: those weekly bars
continue waiting for successors. Continuous markets have no midnight physical-break
exception. Sub-hour closures cannot be inferred from hourly timestamps; learned
ends conservatively use the last hourly opening plus one hour.

Date-specific overrides support known special closes. Example (illustrative only):

```yaml
market_rules:
  exchanges:
    RUS:
      timezone: Europe/Moscow
      timestamp_profile: moex_futures
      symbols:
        SBER: {timestamp_profile: null}
        YDEX: {timestamp_profile: null}
    NASDAQ:
      timezone: America/New_York
      timestamp_profile: null
calendar:
  completed_sessions: 30
  lookback_days: 90
  min_observations: 4
  min_agreement: 0.9
  boundary_overrides:
    "NASDAQ:EXAMPLE":
      1D:
        "2026-09-01T13:30:00Z": "2026-09-01T17:00:00Z"
```

Overrides map the **raw native opening**, not the normalized trading label, to
the actual period close. They still require the asset delay and fresh target data.

New ticker initialization downloads 1h, 4h, 1D, and 1W at `provider.initial_bars`
(currently 5000). Each download retains retry caps 5000/5000/5000/4000/2000, each
bounded by that configured limit, with five-second retry waits. Concurrent
requests share initialization. Successful timeframes persist across failures and
restarts; subsequent requests retry missing timeframes. MCP startup selectively
repairs gaps detected in changed caches. Initialization and gap-repair downloads
can take longer than ordinary calls.

New local artifacts, beside the unchanged OHLCV CSV files:

- `data/ticker_registry.json`: initialized timeframes by canonical ticker.
- `<asset>/calendar.json`: one shared weekday session schedule in the exchange
  timezone, with named start/end times and day offsets for overnight sessions.
  Verified native bar alignments refer to those sessions across 1h/4h/1D/1W;
  reopening times are derived from the schedule unless a verified provider
  exception requires an explicit override. Unknown alignments remain unknown.
- `<asset>/coverage.json`: the latest startup check, each unresolved gap once
  with its repair-attempt flag, and the remaining completion receipt evidence.
  Resolved repair markers and duplicate startup gap snapshots are not stored.
- `<asset>/revisions.jsonl`: changed OHLCV values, timeframe and observation time;
  routine updates of the previously latest cached bar are excluded. Legacy
  timeframe revision logs are consolidated at startup or on the next merge.
- `data/download_coverage.jsonl`: actual response sizes/ranges, overlap and added rows.
- `data/backups/<UTC timestamp>.zip`: cache, metadata and log snapshot before startup downloads.

Legacy `<timeframe>.receipts.json` files are migrated into `coverage.json` at
startup (after the normal backup), or before a storage merge. They are removed
only after the consolidated document is written successfully. Receipt evidence
is pruned conservatively: when a bar's successor is at or before the calendar's
effective date, no historical cutoff can need that receipt. Earlier cutoffs
cannot use that calendar; later cutoffs already have the successor. The latest
bar and all potentially relevant later evidence remain, retaining the request
start time and OHLCV fingerprint. This is deliberately safer than keeping only
one receipt per timeframe. Historical-cutoff completion behavior is preserved. Coverage updates preserve receipt
evidence under the storage write lock. Storage keeps at most 16 parsed coverage
documents in memory, invalidated by file size/modification time, to avoid parsing
all four timeframe receipt maps on every read. Receipt evidence is internal and
does not appear in the public `history_coverage` response.

Bars add `completion_reason` and nullable `completion_boundary`; all endpoints
add `completion_calendar` metadata. Reasons include `successor_received`,
`session_boundary_delay`, `period_boundary_delay`, `awaiting_successor`,
`awaiting_boundary_delay`, `awaiting_fresh_data`, and `calendar_unavailable`.
Existing fields/types are retained. Legacy nominal `bar_close`/`effective_bar_close`
fields retain their prior timestamp convention; `completion_boundary` carries the
physical boundary used by a timeout. Strict consumers asserting exact JSON key
sets must allow the additions. Bars, explicit/live analysis, and charts use the
same completion evaluator; downstream daily indicators receive finalized data.

A historical cutoff limits eligible source bars and successors. A calendar built
today cannot authorize a boundary exception before its effective time. Historical
OHLCV remains reconstructed, potentially revised history, not an immutable record
of what was received live. Live download responses can arrive after their request
timestamp; that transport delay does not invalidate fresh boundary evidence.

### RUS daily trading-date timestamps (2026-09-07)

For assets listed in `provider.rus_daily_trading_date_assets` (initially
`RUS:MX1!` and `RUS:SI1!`), `1D` responses now label each daily
bar with its inferred owning trading date at `00:00Z`. This is a trading-date
label, not the physical session-open time. Raw CSVs and provider timestamps
remain unchanged, as do OHLCV values. `1h`, `4h`, and other assets retain
their existing timestamps. Restart the MCP server to load this change;
`agent_loop.py` requires no changes.

The symbol list is configurable in `config.yaml`:

```yaml
provider:
  rus_daily_trading_date_assets:
    - "RUS:MX1!"
    - "RUS:SI1!"
```

Add another verified RUS symbol to this list and restart MCP to apply it.
The existing setting name is retained for compatibility; it selects both
daily (`1D`) and weekly (`1W`) normalization.
An empty list disables this normalization; omitting the setting retains the
MX1/SI1 defaults. Asset strings are canonicalized; malformed entries, other
exchanges, and duplicate symbols fail configuration loading. The list selects
the existing session rules; it does not automatically make those rules suitable
for every stock on the exchange.

**YDEX is not compatible with this profile.** A direct September 7 check of 500
daily source bars found weekday opens at `04:00Z` and separate weekend bars at
`07:00Z`: September 5, 6, and 7 are three distinct daily rows. It should retain
each calendar date if a separate same-date normalization is implemented.
The MX1/SI1 profile rejects its `07:00Z` daily template instead of relabeling
weekend prices as Monday. YDEX has therefore not been added to the list.

No exchange calendar is used. The standard UTC-hour rules are:

| Source hour | Standard daily date |
| --- | --- |
| `03`, `04`, `05` | Same date |
| `06` (additional session), `15`, `16`, `17` (old evening session) | Next Monday-Friday date |

A following recorded daily session bounds this inference: it cannot be
assigned past that successor's date. This handles recorded working Saturdays
even when old intraday history is absent. Unrecognized source timestamp
patterns return an error rather than being guessed.

Existing local `4h.csv` data can correct or confirm the standard date. Both
the daily open and its successor must be present in the 4h cache; the successor
must have opened, and their aggregate open/high/low must match the daily bar.
Normally all nominal four-hour intervals must end before the successor. A final
session-end bar may cross that nominal boundary only when cached `1h` bars cover
every hour up to the boundary, a successor hourly bar exists, and the pre-boundary
hourly OHLC exactly matches that final 4h bar. Post-boundary prices are not used.
Missing or mismatching hourly evidence retains the standard fallback. The ending
intraday Moscow date then determines ownership. Daily closes may use settlement
prices, and volumes can be revised independently, so neither is a matching
requirement. No additional 4h downloads are initiated. A live daily tail without
a successor always keeps the standard assumption.

`asset_bars` includes `source_timestamp`, `timestamp_basis`,
`timestamp_evidence`, and `timestamp_ambiguous` on normalized daily bars.
Response metadata (`timestamp_normalization`, or `daily_timestamp_normalization`
in analysis) describes the convention and evidence counts. Charts and analysis
use the same normalized view. Original source opens are checked before exposing
rows at a historical cutoff, so moving a morning label to midnight does not make
the bar available before its source session opened.

**Fallback policy:** absent, incomplete, or mismatching 4h evidence retains the
standard date. It is an assumption, not calendar-verified finality. Holiday
ownership can remain wrong until suitable evidence exists. If fallback assigns
two source rows the same date, both are retained in source order and flagged;
use `source_timestamp` as their unique identity. In the audited cache this occurs
for the February 19/22, 2021 source rows in each asset. Never deduplicate by the
normalized `t`. No source rows are excluded to resolve these collisions.

This supplies conservative midnight completion boundaries to consumers using
`t + 24h`. It also means the unchanged agent-loop partial-daily logic cannot
reconstruct pre-Monday weekend prices from the Monday-labeled daily bar; those
prices remain available in the unchanged intraday series. Source revisions and
calendar-free fallback still prevent a guarantee of identical historical/live
classification inputs. This change does not freeze existing episodes.

Validation: `tests/test_trading_dates.py` covers standard rules, holiday and
working-Saturday corrections, unusable evidence, live tails, cutoff eligibility,
collision preservation, and raw-key refreshes. The read-only full-cache audit is
`D:/PY/202607_algo_llm/agent-judge-release-h4/agent_calibrated_v19/research_execution/rus_daily_timestamp_audit.py`;
its September 7 report is in the adjacent `rus_daily_timestamp_audit_20260907/`
directory. All 3,771 MX1 and 5,032 SI1 rows and their OHLCV were preserved;
daily bars, analysis, and charts passed, and both September 7 preweek contexts
end on September 4. Audit files retain raw hashes and mapping provenance.

### RUS weekly timestamps (2026-09-07)

Configured symbols also return `1W` bars stamped Monday `00:00Z` of their owning
trading week. The opening daily bar is matched by raw timestamp and open price;
its inferred trading date (including the local 4h correction when available)
determines the Monday label. Missing or mismatching daily evidence uses the
standard opening-session rule. Only existing local daily/4h caches are consulted;
no auxiliary downloads or calendar lookups are introduced.

For example, raw `2026-08-29T06:00Z` becomes `2026-08-31T00:00Z`, and raw
`2026-09-05T06:00Z` becomes `2026-09-07T00:00Z`. A Monday-morning source opening
also becomes Monday midnight. Original source opens still gate availability.
Consumers using `t + 7 days <= anchor` now retain the prior week exactly at
Monday midnight and exclude the current week. Raw weekly OHLCV and source
timestamps are preserved; this does not rebuild weekly prices from daily bars.

Weekly responses identify convention `rus_weekly_trading_week_v1` and expose
the same per-bar provenance fields as daily responses. Calendar-free historical
fallback can produce repeated labels around exceptional holidays. Both rows
remain, flagged as ambiguous; do not deduplicate by `t`. The full cache audit
found 29 flagged rows per symbol (including propagated daily ambiguity), and
no ambiguous labels in the checked August 31/September 7 weeks. Historical
fallback labels are assumptions, not verified holiday ownership.

The read-only audit preserved all 780 MX1 and 1,082 SI1 weekly rows and OHLCV,
verified Monday-midnight labels and both week cutoffs, and recorded raw CSV hashes
in `D:/PY/202607_algo_llm/agent-judge-release-h4/agent_calibrated_v19/research_execution/rus_weekly_timestamp_audit_20260907.json`.
Restart MCP to load the weekly change. No agent-loop change is required.

September 8 correction: the November 3, 2025 `16:00Z` MX1 daily source bar now
maps to November 5 `00:00Z`, rather than the November 4 fallback. The final
November 5 `13:00Z` 4h bar is verified against hourly bars through `15:00Z`, all
ending by the `16:00Z` successor boundary. This applies to daily normalization
and weekly opening-day evidence, uses only existing local hourly storage, and
does not alter source prices or require agent-loop changes. Restart MCP to load.

An empty timeframe store requests the full configured batch (currently 5,000).
For a tail refresh, estimate bars from the newest cached raw timestamp to the
actual download time, plus `refresh_overlap_bars` (currently five). Never size a
latest-history download from a historical query cutoff: tvDatafeed does not
receive that cutoff. If a small response has no shared timestamps with the cache,
try the full batch. Valid rows are merged by raw timestamp, with fresh values
winning and changes recorded in the revision log.

Historical coverage is checked within the requested count/session/date window,
not inferred from the cache maximum. Unexplained gaps or insufficient historical
reach trigger one full-batch repair attempt. Persisted interval markers prevent
the same unresolved gap causing repeated full downloads; startup rechecks changed
caches and preserves those repair markers.
An exact resumption matching a verified calendar rule can explain a closure;
merely overlapping a weekend cannot. Cross-timeframe activity confirms intraday
holes, while ambiguous daily/weekly ownership remains uncertain. Native daily or
weekly candles can legitimately span holiday tails. No synthetic candles are
inserted. Provider history limits may leave old gaps unresolved.

All endpoints expose `history_coverage` with `no_known_gaps`, `uncertain`, or
`incomplete`, plus unresolved intervals and available range. This is evidence of
coverage, not a guarantee the provider itself has a perfect history. Bar requests
can return partial data with this metadata. Analysis returns
`INSUFFICIENT_HISTORY_COVERAGE` if confirmed missing/unavailable data affects its
calculation history, including its daily auxiliary input. A stale cache without
proof of missing trading bars remains uncertain rather than being called covered.
`data_quality.unexpected_missing_bars` preserves null for unknown cases.

Provider failures are retried five times with a five-second wait between
attempts. Attempts 1–3 use the original request capped at 5,000 bars, attempt 4
caps it at 4,000, and attempt 5 caps it at 2,000.

Every triggered provider download is appended to `data/download_control.csv`.
The control rows contain `asset`, `timeframe`, `requested_at`, `bars_requested`,
`status` (`success` or `failure`), and the UTC `input_timestamp`. Existing rows
from the older schema are preserved with a blank timeframe because it cannot be
inferred reliably. Requests already covered by local data do not trigger a
download and are not logged.

### Holiday reopening segment verification (2026-09-08)
If full-interval RUS daily O/H/L verification fails, normalization may verify only
its final contiguous 4h segment after a gap of at least 28 hours between opens
(at least 24 hours after the preceding nominal 4h close). The segment must start
at 03/04/05/06 UTC, contain at least two consecutive 4h bars on one Moscow date,
reach the next daily opening, and match daily open/high/low AND volume exactly.
Existing hourly verification remains mandatory when the final 4h bar overlaps
the successor opening. Settlement close differences are allowed; source OHLCV
is never rewritten. Evidence is reported as `four_hour_holiday_tail_ohlv_matches`
with `_hourly_session_end` when applicable. Missing/mismatching evidence retains
the existing fallback; this is not a universal holiday-calendar replacement.

This corrects MX1/SI1 source 2025-12-30T16:00Z to daily 2026-01-05T00:00Z and
therefore weekly 2026-01-05T00:00Z, eliminating that December 29 collision and
preventing admission into January 5 pre-week context. Restart MCP to load the
change; no agent-loop/adapter changes or raw-cache migration are needed.

## Shared timestamp profiles v2 (2026-09-08)

`provider.timestamp_profiles` now explicitly maps MX1/SI1 to `moex_futures`,
GC1/NQ1 to `cme_overnight`, BRN1 to `ice_brent`, and BTCUSD to `utc_calendar`.
This mapping supersedes the legacy RUS list when present; `{}` disables all
profiles, while removing the key restores the legacy RUS behavior. Unknown assets
remain raw and are NOT certified for execution. Add symbols only after checking
compatibility with a profile. CME uses America/Chicago and ICE Europe/London for
initial hour-based dates; this handles DST rather than fixing an opening UTC hour.

Daily output `t` is trading-date midnight UTC; weekly output `t` is Monday midnight.
`source_timestamp` preserves the actual source stamp. Intraday times and all raw
storage/prices remain unchanged. Intraday evidence verifies/corrects daily
ownership; absent or mismatching evidence retains the profile's standard date.
Weekly verification checks contributing daily ownership, dates inside the
labelled week and aggregate O/H/L; failed checks are reported, not excluded.
Daily settlement closes/weekly volume revisions are not forced to match. Bitcoin
uses its UTC calendar convention without requiring an intraday cache for daily
labels; weekly validation still requires daily evidence.

Operator-selected policy: retain all eligible bars. Missing intraday history,
mismatching prices or no successor does not remove a bar. Normal evening opens
advance to the next weekday; normal morning opens retain the date. Matching
intraday evidence overrides this fallback. Duplicate labels are preserved and
flagged, never merged. `unverified_rows` and `unverified_source_timestamps` report
verification failures over available history at the cutoff, not just the tail.
Unknown opening patterns are explicitly listed in `unrecognized_source_timestamps`.
The loop ignores ambiguity/completeness flags: retaining fallback bars means
unresolved holiday ownership can still cause future-data leakage. This policy
preserves history; it is not a universal all-history causality guarantee.

Restart MCP to activate. Re-run affected research before comparing with artifacts
created under the old timestamp policy. Raw caches need no migration; consumer
caches keyed by returned timestamps must distinguish v2. No engine/adapter code
or saved episodes are modified.

Profile timezone correction (2026-09-08): intraday-confirmed ending dates use
Europe/Moscow for MOEX, America/Chicago for CME, Europe/London for Brent and UTC
for Bitcoin. Using Moscow for every profile incorrectly advanced Brent weekday
labels when the last 4h opening was 21:00 UTC. Fixed in code; restart required.

### Evening-range fallback (2026-09-08)
Profiles now use local-time ranges including the start and excluding midnight:
MOEX Europe/Moscow 18:00–24:00; CME America/Chicago 16:00–24:00;
Brent Europe/London 22:00–24:00. Opening minutes within the range are accepted.
Evening openings advance to the next weekday labelled at 00:00 UTC; daytime
openings retain their local date. MOEX's separate observed 06:00 UTC additional
session rule remains. Bitcoin retains its UTC calendar convention. Matching 4h
(and required hourly) evidence can override the standard date. Missing or failed
verification keeps the fallback even when it is a holiday, per operator choice.
No bars are dropped. Weekly dates use Monday of the derived daily date; this
corrects the nine legacy GC Sunday21UTC openings in 1983 to the following Monday.
The range definitions live in timestamp_profiles.py::SESSION_PROFILES. Restart
MCP to load this change. Raw storage, OHLCV, loop and adapter are unchanged.

### Exchange/product-specific 4h holiday verification (2026-09-08)
`timestamp_profiles.py::SESSION_PROFILES` owns the evening range,
holiday-tail session shape and volume policy. Timezones now come from the unified
market rules described above (superseding the original profile timezone). MOEX retains daytime
03–06UTC reopening, one local date and exact O/H/L/volume. CME allows an evening
opening in its local range followed by the next local date, bounded to at most
25 elapsed hours (DST allowance). Brent permits that overnight shape or a morning
opening before05 local on one date. Bitcoin disables holiday-tail reinterpretation.
All tail checks retain the post-gap requirement, contiguous4h intervals, at least
two bars, next-daily boundary and existing hourly validation for shortened ends.
CME/Brent require matching O/H/L; volume disagreement is reported in evidence as
`four_hour_holiday_tail_ohl_matches_volume_differs`, not rewritten or used to reject
the ownership match. MOEX's stronger exact-volume requirement remains unchanged.
Failed verification still retains standard fallback per operator instruction.

Stored cases verified: GC July7,2025 / June22,2026 / July6,2026; NQ July7,2025 /
June22,2026; MX/SI January5,2026. Both daily and weekly labels belong to the proper
Monday and fail that Monday's pre-week admission filter. Restart MCP to activate;
this is not a guarantee for other unmatched historical cases.

Calendar schema 2 is a storage format: the runtime derives its existing boundary
lookup from the shared sessions and verified bar alignment. This retains native
provider ownership checks, date overrides, suspended rules, DST handling and the
public completion response shape. Existing files convert on startup after the
usual backup; unchanged calendars retain their effective date and version.

### Reusing simultaneous monitor refreshes

A successful provider fetch is reused for 10 seconds per canonical asset and
native timeframe within one MCP process. The timer starts after the fetch is
successfully stored and uses a monotonic clock. Waiting callers check it under
the synchronizer lock, so monitors can share a fetch without overlapping provider
calls. Each response still applies its own cutoff, count/sessions and completion
rules to the cached prices. Fetch timestamps and completion evidence are never
advanced by reuse. Forming-bar prices may therefore be up to 10 seconds older
than a new fetch would return. Failed fetches do not enable reuse; coverage repair
and disjoint-history fallback still perform their required downloads. Restart MCP
to load this change; the reuse timer is in memory and resets on restart.

# tv-history MCP

Standalone historical asset-analysis MCP server backed by tvDatafeed and local
timeframe-specific CSV files.

Assets are loaded dynamically. TradingView-style `EXCHANGE:SYMBOL` is preferred,
for example `RUS:MX1!`. The existing `SYMBOL:EXCHANGE` format remains accepted
for backward compatibility. A valid request triggers an initial tvDatafeed
download when no local history exists for that asset and timeframe.

## Install and run

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
non-final bar. Its `is_bar_complete` status uses the configured exchange delay;
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

An empty timeframe store requests up to 5,000 bars. A subsequent request
refreshes only when its timestamp is later than the latest stored timestamp for
that timeframe. The refresh size is the estimated number of missing bars plus
10 overlapping bars, capped at 5,000. New and old rows are merged by timestamp,
with fresh provider values winning.

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
`timestamp_profiles.py::SESSION_PROFILES` now owns timezone, evening range,
holiday-tail session shape and volume policy together. MOEX retains daytime
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

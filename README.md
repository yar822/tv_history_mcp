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

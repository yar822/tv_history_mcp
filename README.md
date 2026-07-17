# tv-history MCP

Standalone historical asset-analysis MCP server backed by tvDatafeed and local
1-hour CSV files.

## Configured assets

- `NQ1!:CME_MINI`
- `BTCUSD:BITSTAMP`
- `BRN1!:ICEEUR`
- `GC1!:COMEX`
- `USDRUB.P:RUS`

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
  asset="BTCUSD:BITSTAMP",
  timeframe="1h",
  timestamp="2026-07-01T12:00:00Z"
)
```

```text
asset_chart(
  asset="BTCUSD:BITSTAMP",
  timeframe="4h",
  timestamp="2026-07-01T12:00:00Z",
  days="10"
)
```

`asset_chart` returns a PNG candlestick chart with red/green bodies, wicks, and
volume, plus a metadata block describing coverage and refresh behavior.

Supported timeframes are `1h`, `4h`, `1D`, and `1W`. Higher timeframes are
resampled mechanically from the locally stored 1-hour bars. Market sessions
and exchange calendars are intentionally not applied.

An empty store requests 5,000 hourly bars. A subsequent request refreshes only
when its timestamp is later than the latest stored timestamp. The refresh size
is the estimated missing hours plus 10 overlapping bars, capped at 5,000. New
and old rows are merged by timestamp, with fresh provider values winning.

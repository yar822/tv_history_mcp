from __future__ import annotations

import argparse
import asyncio
import os
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult
from pydantic import Field

from .analysis import AssetAnalysisService
from .bars import AssetBarsService
from .chart import AssetChartService, ChartResult
from .config import load_settings
from .mcp_response import mcp_result
from .provider import TvDatafeedProvider
from .storage import CsvStorage
from .sync import HistorySynchronizer


settings = load_settings()
storage = CsvStorage(settings)
provider = TvDatafeedProvider(settings)
synchronizer = HistorySynchronizer(settings, provider, storage)
service = AssetAnalysisService(settings, synchronizer)
bars_service = AssetBarsService(settings, synchronizer)
chart_service = AssetChartService(settings, synchronizer, storage)

transport_security = TransportSecuritySettings(
    allowed_hosts=[
        "100.123.186.70:*",
        "localhost:*",
        "127.0.0.1:*",
    ],
)

mcp = FastMCP(
    name="TradingView Historical Asset Analysis",
    instructions=(
        "Analyze live or historical market bars, return raw bar series, or generate "
        "candlestick charts. Live analysis and raw bar series may include the latest "
        "non-final bar while derived analysis uses finalized bars only. "
        "Use EXCHANGE:SYMBOL (preferred) or the legacy "
        "SYMBOL:EXCHANGE format. Supported timeframes: 1h, 4h, 1D, and 1W."
    ),
    transport_security=transport_security,
)


@mcp.tool()
async def asset_analysis(
    asset: str,
    timeframe: Literal["1h", "4h", "1D", "1W"] = "1h",
    timestamp: str | None = None,
    response_version: Literal["execution"] = "execution",
    include_indicators: bool = False,
) -> CallToolResult:
    """Return market analysis at a requested time.

    Inputs:
        asset: EXCHANGE:SYMBOL (preferred), for example RUS:MX1!. The legacy
            SYMBOL:EXCHANGE format, for example BTCUSD:BITSTAMP, is also accepted.
        timeframe: 1h, 4h, 1D, or 1W. Default: 1h.
        timestamp: ISO-8601 time cutoff. Default: current UTC time.
        response_version: execution. May be omitted. Default: execution.
        include_indicators: Also include RSI, MACD, SMA200,
            EMA9/20/50, Bollinger Bands, ADX, and momentum change. Default: false.

    With an explicit timestamp, uses only bars whose close is at or before it.
    Without a timestamp, price_data may be the latest received non-final bar;
    indicators, structure, levels, signals, and other derived metrics still use
    finalized bars only. Bar completion uses source confirmation plus the
    configured exchange-specific delay. Execution output contains timestamps
    and bar status, OHLCV and previous-bar data, ATR,
    recent path, market structure and trend state, actionable levels and bar
    signal, week context, weekly VWAP, session data when applicable, and data
    quality. Numeric values use two-decimal precision.
    """
    result = await asyncio.to_thread(
        service.analyze,
        asset,
        timeframe,
        timestamp,
        response_version,
        include_indicators,
    )
    return mcp_result(result)


@mcp.tool()
async def asset_bars(
    asset: str,
    timeframe: Literal["1h", "4h", "1D", "1W"] = "1h",
    timestamp: str | None = None,
    count: int = 100,
    sessions: Annotated[int, Field(ge=1)] | None = None,
) -> CallToolResult:
    """Return raw OHLCV bars, ordered oldest-first.

    Inputs:
        asset: EXCHANGE:SYMBOL (preferred), for example ICEEUR:BRN1!. The legacy
            SYMBOL:EXCHANGE form is also accepted.
        timeframe: 1h, 4h, 1D, or 1W. Default: 1h.
        timestamp: ISO-8601 window cutoff. Default: current UTC time.
        count: Bars counting backward from timestamp, 1-1000. Default: 100.
        sessions: Return all bars from the last N distinct UTC trading dates.
            When supplied, sessions takes precedence over count.

    Returns OHLCV bar objects with t/is_bar_complete/o/h/l/c/v, effective close,
    sessions and bars covered, ATR-14 on the returned series, and gap quality.
    A bar is eligible once its open is at or before the requested cutoff. The latest
    bar may be incomplete; is_bar_complete applies source confirmation plus the
    configured exchange-specific delay after expected close.
    """
    result = await asyncio.to_thread(
        bars_service.get_bars, asset, timeframe, timestamp, count, sessions
    )
    return mcp_result(result)


@mcp.tool()
async def asset_chart(
    asset: str,
    timeframe: Literal["1h", "4h", "1D", "1W"] = "1h",
    timestamp: str | None = None,
    days: int | str = 10,
    response_version: Literal["execution"] = "execution",
    sessions: Annotated[int, Field(ge=1)] | None = None,
) -> CallToolResult:
    """Return a PNG candlestick and volume chart ending at a requested time.

    Inputs:
        asset: EXCHANGE:SYMBOL (preferred), for example RUS:MX1!. The legacy
            SYMBOL:EXCHANGE format, for example BTCUSD:BITSTAMP, is also accepted.
        timeframe: 1h, 4h, 1D, or 1W. Default: 1h.
        timestamp: ISO-8601 chart cutoff. Default: current UTC time.
        days: Calendar days to display, from 1 through 365. Default: 10.
        response_version: execution. May be omitted. Default: execution.
        sessions: Display the last N distinct UTC trading dates. When supplied,
            sessions takes precedence over days.

    Renders only bars confirmed complete by a later received bar or the configured
    exchange-specific delay after expected close. Returns a PNG plus metadata with
    requested/effective times, latest-bar status, sessions covered, requested
    window, chart coverage, last_close, chart_low, chart_high, and price_decimals.
    """
    result = await asyncio.to_thread(
        chart_service.render,
        asset,
        timeframe,
        timestamp,
        days,
        response_version,
        sessions,
    )
    if isinstance(result, dict):
        return mcp_result(result)
    assert isinstance(result, ChartResult)
    return mcp_result(result.metadata, result.image_bytes)


def main() -> None:
    parser = argparse.ArgumentParser(description="TradingView historical analysis MCP server")
    parser.add_argument(
        "transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        nargs="?",
    )
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8010")))
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run()
    else:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()

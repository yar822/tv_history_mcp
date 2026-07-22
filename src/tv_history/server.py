from __future__ import annotations

import argparse
import asyncio
import os
from typing import Literal

from mcp.server.fastmcp import FastMCP, Image

from .analysis import AssetAnalysisService
from .chart import AssetChartService, ChartResult
from .config import load_settings
from .provider import TvDatafeedProvider
from .storage import CsvStorage
from .sync import HistorySynchronizer


settings = load_settings()
storage = CsvStorage(settings)
provider = TvDatafeedProvider(settings)
synchronizer = HistorySynchronizer(settings, provider, storage)
service = AssetAnalysisService(settings, synchronizer)
chart_service = AssetChartService(settings, synchronizer, storage)

mcp = FastMCP(
    name="TradingView Historical Asset Analysis",
    instructions=(
        "Tools to analyze current and historical market data and generate "
        "candlestick charts for assets identified as EXCHANGE:SYMBOL or "
        "SYMBOL:EXCHANGE. "
        "Supported timeframes: 1h, 4h, 1D, and 1W."
    ),
)


@mcp.tool()
async def asset_analysis(
    asset: str,
    timeframe: str = "1h",
    timestamp: str | None = None,
    response_version: Literal["legacy", "execution"] = "legacy",
    include_indicators: bool = False,
) -> dict:
    """Return technical analysis for an asset at a requested time.

    Inputs:
        asset: EXCHANGE:SYMBOL (preferred), for example RUS:MX1!. The legacy
            SYMBOL:EXCHANGE format, for example BTCUSD:BITSTAMP, is also accepted.
        timeframe: 1h, 4h, 1D, or 1W. Default: 1h.
        timestamp: ISO-8601 UTC time. Default: current UTC time.
        response_version: legacy or execution. Default: legacy.
        include_indicators: Include raw indicators in execution output. Default: false.

    The result uses the last completed bar at or before timestamp.
    effective_bar_close identifies that bar. bar_open, bar_close, and
    is_bar_complete describe the bar that supplied price_data.

    execution returns a compact decision-focused response using completed bars
    only. Numeric values use two-decimal precision.
    """
    return await asyncio.to_thread(
        service.analyze,
        asset,
        timeframe,
        timestamp,
        response_version,
        include_indicators,
    )


@mcp.tool()
async def asset_chart(
    asset: str,
    timeframe: str = "1h",
    timestamp: str | None = None,
    days: int | str = 10,
    response_version: Literal["legacy", "execution"] = "legacy",
):
    """Return a PNG candlestick and volume chart ending at a requested time.

    Inputs:
        asset: EXCHANGE:SYMBOL (preferred), for example RUS:MX1!. The legacy
            SYMBOL:EXCHANGE format, for example BTCUSD:BITSTAMP, is also accepted.
        timeframe: 1h, 4h, 1D, or 1W. Default: 1h.
        timestamp: ISO-8601 UTC chart endpoint. Default: current UTC time.
        days: Calendar days to display, from 1 through 365. Default: 10.
        response_version: legacy or execution. Default: legacy.

    Returns a PNG chart and metadata containing the requested time, status of
    the latest rendered bar, effective completed-bar close, and chart coverage.
    execution also returns compact visible-chart reference prices.
    """
    result = await asyncio.to_thread(
        chart_service.render, asset, timeframe, timestamp, days, response_version
    )
    if isinstance(result, dict):
        return result
    assert isinstance(result, ChartResult)
    return [result.metadata, Image(data=result.image_bytes, format="png")]


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

from __future__ import annotations

import argparse
import asyncio
import os

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
service = AssetAnalysisService(settings, synchronizer, storage)
chart_service = AssetChartService(settings, synchronizer, storage)

mcp = FastMCP(
    name="TradingView Historical Asset Analysis",
    instructions=(
        "Historical OHLCV analysis backed by tvDatafeed and local CSV storage. "
        "Configured assets: " + ", ".join(sorted(settings.assets))
    ),
)


@mcp.tool()
async def asset_analysis(
    asset: str,
    timeframe: str = "1h",
    timestamp: str | None = None,
) -> dict:
    """Analyze a configured asset from locally stored historical OHLCV.

    Args:
        asset: SYMBOL:EXCHANGE, for example BTCUSD:BITSTAMP.
        timeframe: One of 1h, 4h, 1D, 1W.
        timestamp: ISO-8601 point in time. Defaults to current UTC time.

    The local 1h CSV is refreshed only when the requested timestamp is later
    than its latest stored timestamp. Refreshes request the estimated missing
    hours plus a 10-bar overlap, then replace duplicate timestamps with fresh
    provider values.
    """
    return await asyncio.to_thread(service.analyze, asset, timeframe, timestamp)


@mcp.tool()
async def asset_chart(
    asset: str,
    timeframe: str = "1h",
    timestamp: str | None = None,
    days: int | str = 10,
):
    """Return a candlestick and volume chart for a configured asset.

    Args:
        asset: SYMBOL:EXCHANGE, for example BTCUSD:BITSTAMP.
        timeframe: One of 1h, 4h, 1D, 1W.
        timestamp: ISO-8601 end point. Defaults to current UTC time.
        days: Calendar days to display, from 1 through 365. Numeric strings are accepted.
    """
    result = await asyncio.to_thread(chart_service.render, asset, timeframe, timestamp, days)
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

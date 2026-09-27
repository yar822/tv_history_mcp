"""Run from the repository root; evidence goes only to --output.

Calls the running MCP normally (which may refresh its caches), then compares
intraday bars with independent live provider downloads and raw wire epochs.
Does not change the OS timezone or restart the server.
"""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
import tvDatafeed.main as upstream

from tv_history.config import load_settings
from tv_history.provider import TvDatafeedProvider
from tv_history.provider_client import UtcTvDatafeed


async def validate(args):
    args.output.mkdir(parents=True, exist_ok=True)
    provider = TvDatafeedProvider(load_settings())
    decoder = UtcTvDatafeed._TvDatafeed__create_df
    captures = []

    def capture(raw, symbol):
        captures.append((raw, symbol))
        return decoder(raw, symbol)

    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "endpoint": args.url, "os_timezone_changed": False, "checks": []}
    async with streamablehttp_client(args.url, timeout=60, sse_read_timeout=180) as (read, write, _):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            report["server"] = initialized.serverInfo.model_dump()
            for asset in ("BITSTAMP:BTCUSD", "RUS:MX1!", "COMEX:GC1!"):
                for timeframe in ("1h", "4h"):
                    result = await session.call_tool("asset_bars", {
                        "asset": asset, "timeframe": timeframe, "count": 8})
                    payload = result.structuredContent
                    if payload is None:
                        payload = json.loads(next(c.text for c in result.content if c.type == "text"))
                    name = asset.replace(":", "_").replace("!", "") + "_" + timeframe
                    (args.output / (name + "_mcp.json")).write_text(json.dumps(payload, indent=2), encoding="utf-8")
                    if result.isError or "error" in payload:
                        raise RuntimeError(f"MCP request failed for {asset}/{timeframe}: {payload.get('error')}")
                    captures.clear()
                    with patch.object(UtcTvDatafeed, "_TvDatafeed__create_df", staticmethod(capture)):
                        direct = await asyncio.to_thread(provider.get_history, asset, timeframe, 20)
                    raw, symbol = captures[-1]
                    # Independent extraction from wire bar vectors, bypassing
                    # the production JSON decoder's timestamp conversion.
                    epochs = [float(v) for v in re.findall(r'"v"\s*:\s*\[\s*(-?\d+(?:\.\d+)?)', raw)]
                    expected = [datetime.fromtimestamp(v, timezone.utc).isoformat() for v in epochs]
                    actual = [v.isoformat() for v in direct.index]
                    baseline = decoder(raw, symbol)
                    replay = {}
                    for offset in (0, 3, -5):
                        class LocalClock:
                            @staticmethod
                            def fromtimestamp(value, tz=None):
                                stamp = datetime.fromtimestamp(value, timezone.utc)
                                return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None) + timedelta(hours=offset)
                        with patch.object(upstream, "datetime", SimpleNamespace(datetime=LocalClock)):
                            replay[str(offset)] = decoder(raw, symbol).equals(baseline)
                    checks = []
                    for bar in payload["bars"]:
                        stamp = pd.Timestamp(bar["t"])
                        exists = stamp in direct.index
                        prices_match = None
                        if exists and bar["is_bar_complete"]:
                            row = direct.loc[stamp]
                            prices_match = all(abs(float(row[column]) - bar[key]) < 1e-8
                                               for column, key in zip(("open", "high", "low", "close"), "ohlc"))
                        checks.append({"timestamp": bar["t"], "matches_direct_timestamp": exists,
                                       "complete": bar["is_bar_complete"], "closed_ohlc_matches": prices_match})
                    check = {"asset": asset, "timeframe": timeframe,
                             "raw_epoch_count": len(epochs), "raw_epochs_match_utc": bool(epochs) and expected == actual,
                             "raw_epochs": epochs, "direct_utc_timestamps": actual,
                             "simulated_offset_replays": replay, "bars": checks}
                    check["passed"] = (check["raw_epochs_match_utc"] and all(replay.values())
                                       and len(checks) == 8 and all(b["matches_direct_timestamp"] for b in checks)
                                       and sum(b["closed_ohlc_matches"] is True for b in checks) >= 3
                                       and not any(b["closed_ohlc_matches"] is False for b in checks))
                    report["checks"].append(check)
                    print(json.dumps({k: check[k] for k in ("asset", "timeframe", "passed", "raw_epochs_match_utc")}), flush=True)
                    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["passed"] = all(c["passed"] for c in report["checks"])
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not report["passed"]:
        raise SystemExit("Validation found mismatches; inspect report.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8010/mcp")
    parser.add_argument("--output", required=True, type=Path)
    asyncio.run(validate(parser.parse_args()))

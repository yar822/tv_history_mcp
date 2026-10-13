"""Validate weekly closing confirmation through the restarted HTTP MCP."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    output = Path("data/live_checks/gold_weekly_close_20260928/report.json")
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "queries": [], "checks": []}
    cases = [
        ("2026-09-18T19:30:00Z", "2026-09-18T19:00:00Z", False, "awaiting_successor"),
        ("2026-09-18T20:30:00Z", "2026-09-18T20:00:00Z", False, "awaiting_successor"),
        ("2026-09-18T21:00:00Z", "2026-09-18T20:00:00Z", True, "weekly_close_successor_confirmed"),
        ("2026-09-18T21:15:00Z", "2026-09-18T20:00:00Z", True, "weekly_close_successor_confirmed"),
        ("2026-09-21T01:00:00Z", "2026-09-18T20:00:00Z", True, "successor_received"),
    ]
    def check(name, passed):
        report["checks"].append({"name": name, "passed": bool(passed)})

    async with streamablehttp_client("http://127.0.0.1:8010/mcp", timeout=30, sse_read_timeout=30) as (read, write, _):
        async with ClientSession(read, write) as client:
            report["server"] = (await client.initialize()).serverInfo.model_dump()
            for cutoff, target, complete, reason in cases:
                response = await client.call_tool("asset_bars", {
                    "asset": "COMEX:GC1!", "timeframe": "1h", "timestamp": cutoff, "count": 10})
                payload = response.structuredContent or json.loads(next(c.text for c in response.content if c.type == "text"))
                report["queries"].append({"cutoff": cutoff, "response": payload})
                check(cutoff + " response", not response.isError and "error" not in payload)
                bars = payload.get("bars", [])
                bar = next((b for b in bars if b["t"] == target), {})
                check(cutoff + " completion", bar.get("is_bar_complete") is complete and bar.get("completion_reason") == reason)
                check(cutoff + " price cutoff", bool(bars) and all(b["t"] <= cutoff for b in bars))
                if reason == "weekly_close_successor_confirmed":
                    check(cutoff + " weekly boundary", bar.get("completion_boundary") == "2026-09-18T21:00:00+00:00")
                if cutoff.startswith("2026-09-18"):
                    check(cutoff + " final returned bar", bool(bars) and bars[-1]["t"] == target)
    original = json.loads(Path("data/investigations/gold_closing_confirmation_20260928.json").read_text(encoding="utf-8"))
    original_bar = original["queries"][0]["response"]["bars"][-1]
    for query in report["queries"][1:]:
        bar = next(b for b in query["response"]["bars"] if b["t"] == "2026-09-18T20:00:00Z")
        check(query["cutoff"] + " unchanged OHLCV", all(bar[k] == original_bar[k] for k in ("o", "h", "l", "c", "v")))
    report["passed"] = all(c["passed"] for c in report["checks"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "checks": report["checks"], "report": str(output)}, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

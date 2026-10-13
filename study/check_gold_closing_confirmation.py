"""Read-only live MCP reproduction of Gold's September 18 closing finality."""
import asyncio
import json
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    report = {}
    async with streamablehttp_client("http://127.0.0.1:8010/mcp", timeout=30, sse_read_timeout=30) as (read, write, _):
        async with ClientSession(read, write) as client:
            report["server"] = (await client.initialize()).serverInfo.model_dump()
            report["queries"] = []
            for cutoff in ["2026-09-18T21:00:00Z", "2026-09-18T21:15:00Z", "2026-09-21T01:00:00Z"]:
                response = await client.call_tool("asset_bars", {"asset": "COMEX:GC1!", "timeframe": "1h", "timestamp": cutoff, "count": 10})
                payload = response.structuredContent or json.loads(next(c.text for c in response.content if c.type == "text"))
                report["queries"].append({"cutoff": cutoff, "is_error": response.isError, "response": payload})
    output = Path("data/investigations/gold_closing_confirmation_20260928.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    bars = [next(b for b in q["response"]["bars"] if b["t"] == "2026-09-18T20:00:00Z") for q in report["queries"]]
    assert not any(q["is_error"] for q in report["queries"])
    assert [b["is_bar_complete"] for b in bars] == [False, False, True]
    assert [b["completion_reason"] for b in bars] == ["awaiting_successor", "awaiting_successor", "successor_received"]
    assert len({tuple(b[k] for k in ("o", "h", "l", "c", "v")) for b in bars}) == 1
    print(f"Verified identical OHLCV and incomplete/incomplete/complete flags: {output}")


if __name__ == "__main__":
    asyncio.run(main())

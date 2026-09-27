"""Read saved calendar schemas and perform a live MCP handshake only."""
import argparse
import asyncio
import json
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8010/mcp")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {"calendars": []}
    for path in sorted(Path("data").glob("*/*/calendar.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        rule = doc.get("trading_weeks", {})
        assert rule.get("schema_version") == 2 and "intervals" not in rule, str(path)
        report["calendars"].append({"asset": doc.get("asset"), "path": str(path),
            "schema": rule["schema_version"], "status": rule["status"],
            "built_at_utc": rule.get("built_at_utc"), "open": rule.get("open"), "close": rule.get("close")})
    async with streamablehttp_client(args.url, timeout=15, sse_read_timeout=15) as (read, write, _):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            report["server"] = initialized.serverInfo.model_dump()
            report["tools"] = [t.name for t in (await session.list_tools()).tools]
    report["validation"] = "Live initialize/list_tools and read-only saved metadata inspection; no price tool calls"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

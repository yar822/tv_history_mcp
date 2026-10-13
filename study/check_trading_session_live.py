"""Validate weekly/daily schedules through the running HTTP MCP transport."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from tv_history.config import load_settings
from tv_history.storage import CsvStorage


ASSETS = ["RUS:SI1!", "RUS:MX1!", "COMEX:GC1!", "ICEEUR:BRN1!",
          "CME_MINI:NQ1!", "BITSTAMP:BTCUSD"]
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def position(boundary):
    hour, minute, second = map(int, boundary["time"].rstrip("Z").split(":"))
    assert 0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60
    return DAYS.index(boundary["day"]) * 86400 + hour * 3600 + minute * 60 + second


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8010/mcp")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    store = CsvStorage(load_settings())
    paths = [p for asset in ASSETS for p in store.path_for(asset).parent.iterdir() if p.is_file()]
    paths += [store.settings.data_root / "download_control.csv", store.settings.data_root / "ticker_registry.json"]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.exists()}
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "checks": [], "results": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def check(name, passed):
        report["checks"].append({"name": name, "passed": bool(passed)})
        save()
        print(name, "PASS" if passed else "FAIL", flush=True)

    async with streamablehttp_client(args.url, timeout=30, sse_read_timeout=30) as (read, write, _):
        async with ClientSession(read, write) as client:
            report["server"] = (await client.initialize()).serverInfo.model_dump()
            tools = (await client.list_tools()).tools
            tool = next((t for t in tools if t.name == "trading_session"), None)
            check("tool_registered", tool is not None)
            if tool is None:
                raise RuntimeError("Restarted server does not expose trading_session")
            report["input_schema"] = tool.inputSchema
            props = tool.inputSchema["properties"]
            check("optional_date_schema", set(props) == {"asset", "session", "date"}
                  and props["date"].get("default") is None
                  and props["session"].get("default") == "week"
                  and props["session"].get("enum") == ["week", "daily"])
            for asset in ASSETS:
                calendar = json.loads((store.path_for(asset).parent / "calendar.json").read_text(encoding="utf-8"))
                for mode in ("week", "daily"):
                    # Omit session for weekly calls to exercise the advertised default.
                    arguments = {"asset": asset, **({"session": "daily"} if mode == "daily" else {})}
                    response = await client.call_tool("trading_session", arguments)
                    payload = response.structuredContent or json.loads(next(c.text for c in response.content if c.type == "text"))
                    report["results"].append(payload)
                    prefix = f"{asset} {mode}"
                    check(prefix + " response", not response.isError and "error" not in payload
                          and payload.get("asset") == asset and payload.get("session") == mode)
                    if response.isError or "error" in payload:
                        continue
                    expected = "recurring_rule" if mode == "week" else (
                        "unavailable" if asset == "BITSTAMP:BTCUSD" else "partial")
                    check(prefix + " status", payload.get("status") == expected)
                    check(prefix + " holiday_note", any("holidays" in n and "unknown" in n for n in payload.get("notes", [])))
                    if expected == "unavailable":
                        check(prefix + " no_daily_breaks", payload.get("reason") == "no_learned_daily_sessions" and payload["sessions"] == [])
                        continue
                    metadata = payload["metadata"]
                    offset = int(datetime.now(timezone.utc).astimezone(ZoneInfo(payload["local_timezone"])).utcoffset().total_seconds())
                    windows = payload["sessions"]
                    check(prefix + " utc_local_equivalence", payload["timezone"] == "UTC" and all(
                        w[part]["time"].endswith("Z") and position(w[part]) == (position(w["local"][part]) - offset) % (7 * 86400)
                        for w in windows for part in ("open", "close")))
                    check(prefix + " construction", metadata.get("source") == "cached_1h_timestamps"
                          and metadata.get("utc_basis") == "current_exchange_offset"
                          and metadata.get("close_basis") == "last_hourly_open_plus_1h")
                    if mode == "week":
                        rule = calendar["trading_weeks"]
                        check(prefix + " saved_rule", len(windows) == 1 and all(
                            position(windows[0]["local"][part]) == (rule[part]["day_offset"] * 86400
                            + int(rule[part]["time"][:2]) * 3600 + int(rule[part]["time"][3:]) * 60) % (7 * 86400)
                            for part in ("open", "close")) and metadata.get("matching_weeks") == rule["matching_weeks"])
                    else:
                        check(prefix + " saved_template_count", len(windows) == sum(len(v) for v in calendar["sessions"].values())
                              and metadata.get("coverage") == "observed_templates_only")
    report["changed_files"] = [p for p, digest in before.items() if hashlib.sha256(Path(p).read_bytes()).hexdigest() != digest]
    check("runtime_files_unchanged", not report["changed_files"])
    report["passed"] = all(c["passed"] for c in report["checks"])
    save()
    print("Overall:", report["passed"], flush=True)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

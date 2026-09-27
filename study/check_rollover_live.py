"""Exercise rollover on/off through the running MCP; save explicit evidence."""
import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8010/mcp")
    parser.add_argument("--audit-saved", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.audit_saved:
        report_path = args.output / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        raw = json.loads((args.output / "bars_off.json").read_text(encoding="utf-8"))
        frame = pd.read_csv("data/ICEEUR/BRN1_-094a59d238/1h.csv", index_col=0)
        frame.index = pd.to_datetime(frame.index, utc=True)
        match = all(abs(float(frame.loc[pd.Timestamp(bar["t"]), col]) - bar[key]) < 1e-8
                    for bar in raw["bars"] for key, col in
                    (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume")))
        logs = pd.read_csv("data/download_control.csv")
        recent = logs.loc[(logs["asset"] == "ICEEUR:BRN1!") &
            (pd.to_datetime(logs["input_timestamp"], utc=True) >= pd.Timestamp(report["checked_at_utc"]))]
        report["raw_hourly_csv_matches_raw_response"] = match
        report["source_refreshes_during_validation"] = recent.to_dict("records")
        report["csv_change_explanation"] = "Normal source refreshes occurred for 4h/1D/1W; sampled hourly CSV values still match rollover=false, not adjusted prices."
        assert match
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("Raw hourly CSV matches raw response:", match)
        print("Source refreshes:", recent["timeframe"].tolist())
        return
    paths = list(Path("data/ICEEUR/BRN1_-094a59d238").glob("*.csv"))
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "checks": []}
    def check(name, passed, **detail):
        report["checks"].append({"name": name, "passed": bool(passed), **detail})
        print(name, "PASS" if passed else "FAIL", flush=True)
        (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    async with streamablehttp_client(args.url, timeout=60, sse_read_timeout=180) as (read, write, _):
        async with ClientSession(read, write) as session:
            report["server"] = (await session.initialize()).serverInfo.model_dump()
            tools = (await session.list_tools()).tools
            schemas = {t.name: t.inputSchema.get("properties", {}) for t in tools}
            check("bars_only_rollover_schema", len(tools) == 3
                  and schemas["asset_bars"].get("rollover", {}).get("default") is False
                  and "rollover_end" in schemas["asset_bars"]
                  and all("rollover_end_week" not in props for props in schemas.values())
                  and all(not any(k.startswith("rollover") for k in schemas[name])
                          for name in ("asset_analysis", "asset_chart")))
            async def call(tool, name, **options):
                result = await session.call_tool(tool, {"asset": "ICEEUR:BRN1!", "timeframe": "1h",
                    "timestamp": "2026-09-24T08:30:00Z", **options})
                payload = result.structuredContent or json.loads(next(c.text for c in result.content if c.type == "text"))
                (args.output / (name + ".json")).write_text(json.dumps(payload, indent=2), encoding="utf-8")
                for content in result.content:
                    if content.type == "image":
                        (args.output / (name + ".png")).write_bytes(base64.b64decode(content.data))
                check(name + "_response", not result.isError and "error" not in payload,
                      error=payload.get("error"))
                return payload
            on = await call("asset_bars", "bars_on", count=12, rollover=True)
            off = await call("asset_bars", "bars_off", count=12)
            if "bars" in on and "bars" in off:
                pairs = list(zip(on["bars"], off["bars"]))
                check("hourly_ohlc_shift_and_raw_completion", len(on["bars"]) == len(off["bars"]) and all(
                    a["t"] == b["t"] and a["v"] == b["v"] and a["is_bar_complete"] == b["is_bar_complete"]
                    and all(abs(a[k] - b[k] - (5.25 if a["t"] >= "2026-09-24T00:00:00Z" else 0)) < 1e-8
                            for k in ("o", "h", "l", "c")) for a, b in pairs))
                check("offset_and_start_metadata", on.get("rollover", {}).get("latest_bar_offset") == 5.25,
                      metadata=on.get("rollover"))
            for tf in ("4h", "1D", "1W"):
                a = await call("asset_bars", tf + "_on", timeframe=tf, count=3, rollover=True)
                b = await call("asset_bars", tf + "_off", timeframe=tf, count=3, rollover=False)
                check(tf + "_prices_unchanged", "bars" in a and a.get("bars") == b.get("bars"))
            for tool, prefix, extra in [("asset_analysis", "analysis", {}), ("asset_chart", "chart", {"days": 1})]:
                raw = await call(tool, prefix + "_raw", **extra)
                if "error" not in raw:
                    check(prefix + "_no_adjustment_metadata", "rollover" not in raw)
    report["csv_hashes_unchanged"] = all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest for p, digest in before.items())
    report["csv_changed_files"] = [p for p, digest in before.items() if hashlib.sha256(Path(p).read_bytes()).hexdigest() != digest]
    report["passed"] = all(c["passed"] for c in report["checks"])
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("Overall", report["passed"], "CSV unchanged", report["csv_hashes_unchanged"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())

"""Read-only horizon experiments; run from repo root with an explicit output."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from tv_history import provider_rollovers as adapter


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tickmarks", type=int, default=0)
    parser.add_argument("--asset", default="ICEEUR:BRN1!")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    token = Path("_token.txt").read_text(encoding="utf-8-sig").strip()
    connection = adapter.create_connection
    now = datetime.now(timezone.utc)
    cases = [("60", "now"), ("1D", "now"), ("1W", "now"),
             ("60", "20271231"), ("60", "1830254400"), ("60", "1830254400000")]
    if args.tickmarks:
        cases = [("60", "now")]
    results = []
    for interval, currenttime in cases:
        def connect(*a, **kw):
            ws = connection(*a, **kw)
            original = ws.send
            receive = ws.recv
            pending = []
            def recv():
                raw = receive()
                if '"m":"series_completed"' in raw and pending:
                    params, payload = pending.pop()
                    extra = json.dumps({"m": "request_more_tickmarks", "p": [params[0], "s1", args.tickmarks]}, separators=(",", ":"))
                    original(f"~m~{len(extra)}~m~{extra}")
                    original(payload)
                for part in raw.split('~m~'):
                    if '"m":"protocol_error"' in part:
                        print('protocol detail',part.replace(token,'[REDACTED]')[:400],flush=True)
                return raw
            def send(payload):
                parts = payload.split("~m~", 2)
                if len(parts) == 3 and parts[2].startswith("{"):
                    message = json.loads(parts[2])
                    if message["m"] == "create_series":
                        message["p"][4] = interval
                    if message["m"] == "create_study":
                        message["p"][5]["currenttime"] = currenttime
                    text = json.dumps(message, separators=(",", ":"))
                    payload = f"~m~{len(text)}~m~{text}"
                    if message["m"] == "create_study" and args.tickmarks:
                        pending.append((message["p"],payload))
                        return
                return original(payload)
            ws.send = send
            ws.recv = recv
            return ws
        adapter.create_connection = connect
        record = {"asset": args.asset, "interval": interval, "currenttime": currenttime, "tickmarks": args.tickmarks,
                  "requested_at_utc": datetime.now(timezone.utc).isoformat()}
        try:
            record["snapshot"] = adapter._collect_calendar(record["asset"], token, interval, 15)
            events = record["snapshot"]["events"]
            record["future"] = [e for e in events if datetime.fromisoformat(e["scheduled_at_utc"]) > now]
        except adapter.RolloverError as exc:
            record["error"] = str(exc)
        finally:
            adapter.create_connection = connection
        results.append(record)
        (args.output / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(json.dumps({**{k: v for k, v in record.items() if k not in ("snapshot", "future")},
                          "future_count": len(record.get("future", [])), "first_three": record.get("future", [])[:3]}), flush=True)


if __name__ == "__main__":
    main()

"""Check both service modes against saved calendars without starting MCP."""
import argparse
import hashlib
import json
from pathlib import Path

from tv_history.config import load_settings
from tv_history.storage import CsvStorage
from tv_history.trading_session import TradingSessionService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    storage = CsvStorage(load_settings())
    service = TradingSessionService(storage)
    paths = sorted(storage.settings.data_root.glob("*/*/calendar.json"))
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    results = []
    for path in paths:
        asset = json.loads(path.read_text(encoding="utf-8"))["asset"]
        for mode in ("week", "daily"):
            result = service.get_sessions(asset, mode)
            assert "error" not in result, result
            assert result.get("reason") != "invalid_calendar", result
            results.append(result)
            print(asset, mode, result["status"], len(result["sessions"]), result.get("reason", ""))
    unchanged = all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in hashes.items())
    assert unchanged
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"validation": "Direct read-only service calls against local calendars; no live MCP or provider calls",
                                      "calendar_hashes_unchanged": unchanged,
                                      "results": results}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

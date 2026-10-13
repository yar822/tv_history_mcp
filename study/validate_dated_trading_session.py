"""Read-only dated service checks against existing hourly caches; no live MCP."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd

from tv_history.config import load_settings
from tv_history.storage import CsvStorage
from tv_history.trading_session import TradingSessionService


ASSETS = ["RUS:SI1!", "RUS:MX1!", "COMEX:GC1!", "ICEEUR:BRN1!", "CME_MINI:NQ1!", "BITSTAMP:BTCUSD"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    storage = CsvStorage(load_settings())
    now = datetime.now(timezone.utc)
    service = TradingSessionService(storage, lambda: now)
    paths = [p for asset in ASSETS for p in storage.path_for(asset).parent.iterdir() if p.is_file()]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    results = []
    for asset in ASSETS:
        for mode in ("week", "daily"):
            assert service.get_sessions(asset, mode) == service.get_sessions(asset, mode, None)
            for date in ("2025-09-28", "2026-09-27", "2026-10-04", "2026-12-06"):
                result = service.get_sessions(asset, mode, date)
                assert "error" not in result, result
                monday = pd.Timestamp(date) - pd.Timedelta(days=pd.Timestamp(date).weekday())
                assert result["week"] == str(monday.date()), result
                assert result["status"] in {"observed", "expected", "unavailable"}, result
                if result["status"] == "unavailable":
                    assert asset == "BITSTAMP:BTCUSD" and mode == "daily", result
                else:
                    assert result["sessions"], result
                    assert result["metadata"]["utc_basis"] == "boundary_dates"
                    for window in result["sessions"]:
                        assert window["basis"] == result["status"]
                        for part in ("open", "close"):
                            assert pd.Timestamp(window[part]["datetime"]) == pd.Timestamp(window["local"][part]["datetime"])
                        assert pd.Timestamp(window["open"]["datetime"]) < pd.Timestamp(window["close"]["datetime"])
                    if result["status"] == "expected":
                        assert result["metadata"]["expectation_reason"]
                        assert result["metadata"]["history_fallback"] == (result["metadata"]["week_state"] == "ended")
                results.append(result)
                print(asset, mode, date, result["status"], result.get("metadata", {}).get("expectation_reason", ""), flush=True)
    unchanged = all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest for p, digest in before.items())
    assert unchanged
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"validation": "Direct local-cache service checks only; no provider or live MCP calls",
                                      "checked_at_utc": now.isoformat(), "runtime_files_unchanged": unchanged,
                                      "results": results}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

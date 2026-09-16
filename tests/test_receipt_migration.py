import json
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from tv_history.storage import CsvStorage, atomic_json
from test_sync import make_settings, frame

ASSET = "RUS:MX1!"
FETCHED = pd.Timestamp("2026-09-01T02:05Z")


def test_receipts_live_in_coverage_and_survive_metadata_saves(tmp_path):
    store = CsvStorage(make_settings(tmp_path))
    prices = frame("2026-09-01", [100., 101.])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda tf: store.merge_and_write(ASSET, prices, tf, FETCHED),
                      ("1h", "4h", "1D", "1W")))
    store.save_coverage(ASSET, {"startup_check": {"checked": True}})
    folder = store.path_for(ASSET).parent
    doc = json.loads((folder / "coverage.json").read_text())
    assert set(doc["receipt_evidence"]) == {"1h", "4h", "1D", "1W"}
    assert not list(folder.glob("*.receipts.json"))
    assert "receipt_evidence" not in store.read_coverage(ASSET)
    key = prices.index[0].isoformat()
    returned = store.read(ASSET)
    returned.attrs["receipts"][key]["ohlcv"][3] = -1
    assert store.read(ASSET).attrs["receipts"][key]["ohlcv"][3] == 100.


def test_legacy_migration_preserves_evidence_and_existing_metadata(tmp_path):
    store = CsvStorage(make_settings(tmp_path))
    path = store.path_for(ASSET)
    path.parent.mkdir(parents=True)
    receipt = {"request_started_at": FETCHED.isoformat(), "received_at": FETCHED.isoformat(),
               "ohlcv": [100., 101., 99., 100., 100.]}
    mapping = {"2026-09-01T00:00:00+00:00": receipt}
    atomic_json(path.parent / "coverage.json", {"startup_check": {"files": "unchanged"}})
    for tf in ("1h", "4h", "1D", "1W"):
        atomic_json(path.parent / f"{tf}.receipts.json", mapping)
    store.migrate_metadata(ASSET)
    first = (path.parent / "coverage.json").read_bytes()
    store.migrate_metadata(ASSET)
    assert (path.parent / "coverage.json").read_bytes() == first
    doc = json.loads(first)
    assert all(value == mapping for value in doc["receipt_evidence"].values())
    assert doc["startup_check"] == {"files": "unchanged"}
    assert not list(path.parent.glob("*.receipts.json"))


def test_failed_migration_retains_legacy_files(tmp_path, monkeypatch):
    store = CsvStorage(make_settings(tmp_path))
    legacy = store.path_for(ASSET).with_suffix(".receipts.json")
    atomic_json(legacy, {})
    def fail(*args):
        raise OSError("write failed")
    monkeypatch.setattr(store, "_write_coverage_document", fail)
    with pytest.raises(OSError, match="write failed"):
        store.migrate_metadata(ASSET)
    assert legacy.exists()


def test_external_metadata_update_invalidates_cached_document(tmp_path):
    store = CsvStorage(make_settings(tmp_path))
    store.save_coverage(ASSET, {"state": "before"})
    path = store.path_for(ASSET).parent / "coverage.json"
    atomic_json(path, {"state": "after", "receipt_evidence": {"1h": {}}})
    assert store.read_coverage(ASSET) == {"state": "after"}
    store.save_coverage(ASSET, {"state": "latest"})
    assert json.loads(path.read_text())["receipt_evidence"] == {"1h": {}}

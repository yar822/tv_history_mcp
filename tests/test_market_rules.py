from dataclasses import replace

import pandas as pd
import pytest
import yaml

from tv_history.config import load_settings
from tv_history.market_rules import parse_market_rules, resolve_market_rule
from tv_history.calendar import learn_calendar
from tv_history.storage import CsvStorage
from tv_history.sync import HistorySynchronizer
from test_sync import make_settings
from test_calendar_finality import native_frames, Provider
from test_trading_dates import frame


@pytest.mark.parametrize("asset,zone,profile", [
    ("RUS:MX1!", "Europe/Moscow", "moex_futures"),
    ("si1!:rus", "Europe/Moscow", "moex_futures"),
    ("RUS:NEW1!", "Europe/Moscow", "moex_futures"),
    ("sber:rus", "Europe/Moscow", None),
    ("RUS:YDEX", "Europe/Moscow", None),
    ("COMEX:GC1!", "America/Chicago", "cme_overnight"),
    ("CME_MINI:NQ1!", "America/Chicago", "cme_overnight"),
    ("ICEEUR:BRN1!", "Europe/London", "ice_brent"),
    ("BITSTAMP:BTCUSD", "UTC", "utc_calendar"),
    ("NASDAQ:ABC", None, None),
])
def test_configured_defaults_and_exceptions(asset, zone, profile):
    rule = resolve_market_rule(load_settings(), asset)
    assert (rule.timezone, rule.timestamp_profile) == (zone, profile)


def test_symbol_overrides_each_field_independently():
    cfg = replace(load_settings(), market_rules=parse_market_rules({"exchanges": {
        " rus ": {"timezone": "Europe/Moscow", "timestamp_profile": "moex_futures", "symbols": {
            "sber": {"timestamp_profile": None},
            "TEST": {"timezone": "Europe/London"},
            "ALT": {"timezone": "America/Chicago", "timestamp_profile": "cme_overnight"},
        }}}}))
    assert resolve_market_rule(cfg, "RUS:SBER").timestamp_profile is None
    rule = resolve_market_rule(cfg, "TEST:RUS")
    assert (rule.timezone, rule.timestamp_profile) == ("Europe/London", "moex_futures")
    rule = resolve_market_rule(cfg, "RUS:ALT")
    assert (rule.timezone, rule.timestamp_profile) == ("America/Chicago", "cme_overnight")


@pytest.mark.parametrize("tf", ["1D", "1W"])
def test_default_normalizes_and_null_exception_preserves_native_labels(tmp_path, tf):
    cfg = replace(load_settings(), data_root=tmp_path / "data")
    sync = HistorySynchronizer(cfg, Provider(), CsvStorage(cfg))
    raw = frame(["2026-03-02T15:00Z", "2026-03-09T15:00Z"])
    cutoff = pd.Timestamp("2026-03-10T00:00Z")
    for asset in ("RUS:SBER", "RUS:YDEX", "NASDAQ:ABC"):
        result = sync._normalize_view(asset, tf, raw, cutoff)
        pd.testing.assert_frame_equal(result, raw)
        assert "timestamp_normalization" not in result.attrs
    for asset in ("RUS:MX1!", "RUS:SI1!", "RUS:NEW1!"):
        result = sync._normalize_view(asset, tf, raw, cutoff)
        assert "timestamp_normalization" not in result.attrs
        expected = "2026-03-03T00:00Z" if tf == "1D" else "2026-03-02T00:00Z"
        assert result.index[0] == pd.Timestamp(expected)
        assert result.iloc[0].source_timestamp == "2026-03-02T15:00:00Z"


def test_calendar_and_normalizer_use_same_overridden_timezone(tmp_path):
    cfg = replace(make_settings(tmp_path), market_rules=parse_market_rules({"exchanges": {
        "COMEX": {"timezone": "America/Chicago", "timestamp_profile": "cme_overnight",
                  "symbols": {"TEST": {"timezone": "UTC"}}}}}))
    sync = HistorySynchronizer(cfg, Provider(), CsvStorage(cfg))
    raw = frame(["2026-03-02T17:00Z", "2026-03-03T17:00Z"])
    cutoff = pd.Timestamp("2026-09-01T00:00Z")
    result = sync._normalize_view("COMEX:TEST", "1D", raw, cutoff)
    # 17:00 UTC is past 16:00 in the override zone, but before 16:00 Chicago.
    assert result.index[0] == pd.Timestamp("2026-03-03T00:00Z")
    cal = learn_calendar("COMEX:TEST", native_frames(), cfg, cutoff)
    assert cal["timezone"] == "UTC"
    assert cal["status"] == "ready"


def test_first_request_and_restart_apply_unified_sber_exception(tmp_path):
    cfg = replace(load_settings(), data_root=tmp_path / "data")
    provider = Provider()
    storage = CsvStorage(cfg)
    sync = HistorySynchronizer(cfg, provider, storage)
    sync.ensure_available("sber:rus", "1D", pd.Timestamp("2026-08-01T00:00Z"))
    assert provider.calls == [("RUS:SBER", tf, 5000) for tf in ("1h", "4h", "1D", "1W")]
    assert sync.calendars["RUS:SBER"]["timezone"] == "Europe/Moscow"
    provider = Provider()
    restarted = HistorySynchronizer(cfg, provider, storage)
    assert restarted.calendars["RUS:SBER"]["timezone"] == "Europe/Moscow"
    assert provider.calls == []


@pytest.mark.parametrize("rule", [
    {"timestamp_profile": "moex_futures"},
    {"timezone": "UTC", "timestamp_profile": "typo"},
    {"timezone": None},
    {"timezone": "Europe/Moscow", "timestamp_profile": "utc_calendar"},
    {"timezone": "UTC", "symbols": {"RUS:SBER": {}}},
    {"timezone": "UTC", "symbols": {"sber": {}, "SBER": {}}},
    {"timezone": "UTC", "symbols": {"SBER": {"profile": None}}},
])
def test_invalid_rules_rejected(rule):
    with pytest.raises(ValueError):
        parse_market_rules({"exchanges": {"RUS": rule}})


def test_unified_config_rejects_ambiguous_legacy_settings(tmp_path):
    content = {"storage": {"root": "data"}, "provider": {"timestamp_profiles": {}},
               "indicators": {}, "market_rules": {"exchanges": {}}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(content))
    with pytest.raises(ValueError, match="cannot be combined"):
        load_settings(path)

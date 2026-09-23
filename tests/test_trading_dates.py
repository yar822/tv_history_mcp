from __future__ import annotations

import pandas as pd
import pytest
from dataclasses import replace
import yaml

from tv_history.bars import AssetBarsService
from tv_history.storage import CsvStorage, empty_frame
from tv_history.sync import HistorySynchronizer
from tv_history.trading_dates import normalize_daily, normalize_weekly, standard_date, uses_trading_dates
from test_sync import FakeProvider, make_settings
from test_analysis import settings as analysis_settings
from tv_history.config import load_settings


def frame(stamps, values=None):
    if values is None:
        values = [[10.0, 12.0, 9.0, 11.0, 100.0] for _ in stamps]
    return pd.DataFrame(values, columns=["open", "high", "low", "close", "volume"],
                        index=pd.DatetimeIndex(stamps, tz="UTC", name="timestamp_utc"))


@pytest.mark.parametrize("raw,expected", [
    ("2026-03-12T16:00Z", "2026-03-13"),
    ("2026-03-13T16:00Z", "2026-03-16"),
    ("2026-03-23T05:00Z", "2026-03-23"),
    ("2026-08-03T03:00Z", "2026-08-03"),
    ("2026-09-05T06:00Z", "2026-09-07"),
    ("2026-06-12T06:00Z", "2026-06-15"),
    ("2012-04-26T15:00Z", "2012-04-27"),
    ("2016-02-18T17:00Z", "2016-02-19"),
])
def test_standard_session_rules(raw, expected):
    assert standard_date(pd.Timestamp(raw)) == pd.Timestamp(expected, tz="UTC")


@pytest.mark.parametrize("asset,timeframe,expected", [
    ("RUS:MX1!", "1D", True), ("SI1!:RUS", "1D", True),
    ("RUS:MX1!", "4h", False), ("RUS:SI1!", "1W", True),
    ("COMEX:GC1!", "1D", False), ("RUS:OTHER", "1D", False),
])
def test_scope(asset, timeframe, expected):
    assert uses_trading_dates(asset, timeframe) is expected


def test_four_hour_evidence_corrects_monday_holiday_to_tuesday():
    daily = frame(["2026-02-20T16:00Z", "2026-02-24T16:00Z"])
    four = frame(["2026-02-20T16:00Z", "2026-02-20T20:00Z",
                  "2026-02-24T04:00Z", "2026-02-24T08:00Z",
                  "2026-02-24T12:00Z", "2026-02-24T16:00Z"])
    result = normalize_daily(daily, four, pd.Timestamp("2026-03-01", tz="UTC"))
    assert result.index[0] == pd.Timestamp("2026-02-24", tz="UTC")
    assert result.iloc[0].timestamp_basis == "four_hour_confirmed"
    pd.testing.assert_frame_equal(daily, frame(list(daily.index)))
    assert list(result.iloc[0][daily.columns]) == list(daily.iloc[0])


def test_working_saturday_corrected_by_four_hour_evidence():
    daily = frame(["2024-04-26T16:00Z", "2024-04-29T04:00Z"])
    four = frame(["2024-04-26T16:00Z", "2024-04-26T20:00Z",
                  "2024-04-27T04:00Z", "2024-04-27T08:00Z",
                  "2024-04-27T12:00Z", "2024-04-29T04:00Z"])
    result = normalize_daily(daily, four, pd.Timestamp("2024-05-01", tz="UTC"))
    assert list(result.index) == list(pd.to_datetime(["2024-04-27", "2024-04-29"], utc=True))


def test_recorded_working_saturday_boundary_without_four_hour_history():
    daily = frame(["2024-11-01T16:00Z", "2024-11-02T16:00Z"])
    result = normalize_daily(daily, empty_frame(), pd.Timestamp("2024-11-06", tz="UTC"))
    assert result.index[0] == pd.Timestamp("2024-11-02", tz="UTC")
    assert result.iloc[0].timestamp_basis == "next_daily_boundary"


@pytest.mark.parametrize("defect", ["missing_first", "missing_successor", "ohl_mismatch", "overlapping"])
def test_unusable_four_hour_evidence_keeps_standard_assumption(defect):
    daily = frame(["2026-02-20T16:00Z", "2026-02-24T16:00Z"])
    four = frame(["2026-02-20T16:00Z", "2026-02-24T12:00Z", "2026-02-24T16:00Z"])
    if defect == "missing_first":
        four = four.iloc[1:]
    elif defect == "missing_successor":
        four = four.iloc[:-1]
    elif defect == "ohl_mismatch":
        four.loc[four.index[1], "high"] = 99
    else:
        four = pd.concat([four, frame(["2026-02-24T14:00Z"])]).sort_index()
    result = normalize_daily(daily, four, pd.Timestamp("2026-03-01", tz="UTC"))
    assert result.index[0] == pd.Timestamp("2026-02-23", tz="UTC")
    assert result.iloc[0].timestamp_basis == "standard_session_rule"


def test_live_weekend_tail_is_not_backdated_by_partial_four_hour_data():
    daily = frame(["2026-09-05T06:00Z"])
    four = frame(["2026-09-05T06:00Z", "2026-09-06T14:00Z"])
    result = normalize_daily(daily, four, pd.Timestamp("2026-09-07T12:00Z"))
    assert result.index[0] == pd.Timestamp("2026-09-07", tz="UTC")
    assert result.iloc[0].timestamp_evidence == "no_successor_daily_bar"


def test_original_open_prevents_exposing_morning_bar_at_midnight():
    daily = frame(["2026-08-03T03:00Z"])
    result = normalize_daily(daily, empty_frame(), pd.Timestamp("2026-08-03T00:00Z"))
    assert result.empty


def test_ambiguous_historical_mapping_never_silently_merges_rows():
    daily = frame(["2021-02-19T16:00Z", "2021-02-22T04:00Z"])
    result = normalize_daily(daily, empty_frame(), pd.Timestamp("2021-02-23", tz="UTC"))
    assert len(result) == 2
    assert list(result.index) == [pd.Timestamp("2021-02-22", tz="UTC")] * 2
    assert result.timestamp_ambiguous.all()
    assert "timestamp_normalization" not in result.attrs
    assert result.source_timestamp.is_unique


def test_service_serializes_both_standard_fallback_rows(tmp_path):
    config = analysis_settings(tmp_path)
    storage = CsvStorage(config)
    raw = frame(["2021-02-19T16:00Z", "2021-02-22T04:00Z", "2021-02-24T16:00Z"])
    storage.merge_and_write("RUS:SI1!", raw, "1D")
    sync = HistorySynchronizer(config, FakeProvider([]), storage)
    response = AssetBarsService(config, sync).get_bars("RUS:SI1!", "1D", "2021-02-23T00:00Z", count=2)
    assert "error" not in response
    assert len(response["bars"]) == 2
    assert all(bar["timestamp_ambiguous"] for bar in response["bars"])
    assert len({bar["source_timestamp"] for bar in response["bars"]}) == 2


def test_unknown_timestamp_is_not_guessed():
    with pytest.raises(ValueError, match="Unrecognized RUS daily"):
        standard_date(pd.Timestamp("2026-09-04T09:00Z"))


def test_service_preserves_raw_csv_and_excludes_monday_from_loop_context(tmp_path):
    config = analysis_settings(tmp_path)
    storage = CsvStorage(config)
    raw = frame(["2026-09-04T03:00Z", "2026-09-05T06:00Z", "2026-09-08T03:00Z"])
    storage.merge_and_write("RUS:MX1!", raw, "1D")
    before = storage.path_for("RUS:MX1!", "1D").read_bytes()
    provider = FakeProvider([raw])
    sync = HistorySynchronizer(config, provider, storage)
    service = AssetBarsService(config, sync)
    response = service.get_bars("RUS:MX1!", "1D", "2026-09-07T00:00Z", count=2)
    assert "error" not in response
    assert [bar["t"] for bar in response["bars"]] == ["2026-09-04T00:00:00Z", "2026-09-07T00:00:00Z"]
    assert response["bars"][-1]["is_bar_complete"] is False
    assert response["bars"][-1]["source_timestamp"] == "2026-09-05T06:00:00Z"
    anchor = pd.Timestamp("2026-09-07T00:00Z")
    retained = [bar for bar in response["bars"] if pd.Timestamp(bar["t"]) + pd.Timedelta(days=1) <= anchor]
    assert [bar["t"] for bar in retained] == ["2026-09-04T00:00:00Z"]
    assert storage.path_for("RUS:MX1!", "1D").read_bytes() == before
    # The unverified gap gets one full history check, preserving native rows.
    assert provider.requests == [("RUS:MX1!", "1D", 5000)]


def test_refresh_still_merges_using_raw_timestamps(tmp_path):
    config = make_settings(tmp_path)
    storage = CsvStorage(config)
    raw = frame(["2026-09-05T06:00Z"])
    storage.merge_and_write("RUS:SI1!", raw, "1D")
    updated = raw.copy()
    updated["close"] = 12.0
    provider = FakeProvider([updated])
    sync = HistorySynchronizer(config, provider, storage)
    result, _ = sync.ensure_available("RUS:SI1!", "1D", pd.Timestamp("2026-09-07T12:00Z"))
    assert result.index[0] == pd.Timestamp("2026-09-07", tz="UTC")
    assert list(storage.read("RUS:SI1!", "1D").index) == list(raw.index)
    assert storage.read("RUS:SI1!", "1D").iloc[0].close == 12.0


def config_file(tmp_path, assets, *, omit=False):
    provider = {} if omit else {"rus_daily_trading_date_assets": assets}
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump({"storage": {"root": str(tmp_path / "data")},
                                    "provider": provider, "indicators": {}}), encoding="utf-8")
    return path


def test_configurable_selection_defaults_and_disable(tmp_path):
    assert load_settings(config_file(tmp_path, None, omit=True)).rus_daily_trading_date_assets == (
        "RUS:MX1!", "RUS:SI1!"
    )
    assert load_settings(config_file(tmp_path, [])).rus_daily_trading_date_assets == ()
    configured = load_settings(config_file(tmp_path, ["mx1!:rus", "rus:TEST"]))
    assert configured.rus_daily_trading_date_assets == ("RUS:MX1!", "RUS:TEST")
    assert uses_trading_dates("TEST:RUS", "1D", configured.rus_daily_trading_date_assets)
    assert not uses_trading_dates("RUS:SI1!", "1D", configured.rus_daily_trading_date_assets)


@pytest.mark.parametrize("assets", [None, "RUS:MX1!", [42], ["COMEX:GC1!"],
                                   ["RUS:MX1!", "MX1!:RUS"], ["MX1!"]])
def test_bad_normalization_settings_fail_early(tmp_path, assets):
    with pytest.raises(ValueError):
        load_settings(config_file(tmp_path, assets))


def test_setting_routes_new_symbol_and_disables_previous_symbols(tmp_path):
    configured = replace(make_settings(tmp_path), rus_daily_trading_date_assets=("RUS:TEST",))
    sync = HistorySynchronizer(configured, FakeProvider([]), CsvStorage(configured))
    raw = frame(["2026-08-29T06:00Z"])
    cutoff = pd.Timestamp("2026-09-01T00:00Z")
    assert sync._served_view("RUS:TEST", "1D", raw, cutoff).index[0] == pd.Timestamp("2026-08-31", tz="UTC")
    pd.testing.assert_frame_equal(sync._served_view("RUS:MX1!", "1D", raw, cutoff), raw)
    assert sync._served_view("RUS:TEST", "1W", raw, cutoff).index[0] == pd.Timestamp("2026-08-31", tz="UTC")
    pd.testing.assert_frame_equal(sync._served_view("RUS:TEST", "4h", raw, cutoff), raw)


def test_ydex_separate_weekend_daily_bars_are_not_routed_by_default(tmp_path):
    configured = make_settings(tmp_path)
    sync = HistorySynchronizer(configured, FakeProvider([]), CsvStorage(configured))
    raw = frame(["2026-09-05T07:00Z", "2026-09-06T07:00Z", "2026-09-07T04:00Z"])
    pd.testing.assert_frame_equal(sync._served_view("RUS:YDEX", "1D", raw, pd.Timestamp("2026-09-07T12:00Z")), raw)
    with pytest.raises(ValueError, match="Unrecognized RUS daily session timestamp"):
        standard_date(raw.index[0])


@pytest.mark.parametrize("source,expected", [
    ("2026-03-06T16:00Z", "2026-03-09"),
    ("2026-03-23T05:00Z", "2026-03-23"),
    ("2026-08-17T03:00Z", "2026-08-17"),
    ("2026-08-29T06:00Z", "2026-08-31"),
    ("2026-09-05T06:00Z", "2026-09-07"),
])
def test_weekly_standard_mapping_without_daily_cache(source, expected):
    raw = frame([source])
    result = normalize_weekly(raw, empty_frame(), empty_frame(), pd.Timestamp("2026-09-07T12:00Z"))
    assert result.index[0] == pd.Timestamp(expected, tz="UTC")
    assert result.iloc[0].source_timestamp == pd.Timestamp(source).isoformat().replace("+00:00", "Z")
    assert result[raw.columns].reset_index(drop=True).equals(raw.reset_index(drop=True))


def test_weekly_uses_corrected_opening_daily_date_not_week_ending_date():
    daily = frame(["2024-04-26T16:00Z", "2024-04-29T04:00Z"])
    four = frame(["2024-04-26T16:00Z", "2024-04-27T12:00Z", "2024-04-29T04:00Z"])
    result = normalize_weekly(daily.iloc[:1], daily, four, pd.Timestamp("2024-05-01", tz="UTC"))
    assert result.index[0] == pd.Timestamp("2024-04-22", tz="UTC")
    assert result.iloc[0].timestamp_basis == "opening_daily_four_hour_confirmed"


def test_weekly_does_not_expose_morning_open_early():
    raw = frame(["2026-08-17T03:00Z"])
    assert normalize_weekly(raw, empty_frame(), empty_frame(), pd.Timestamp("2026-08-17T00:00Z")).empty


def test_weekly_cutoff_and_raw_storage_preserved(tmp_path):
    config = analysis_settings(tmp_path)
    storage = CsvStorage(config)
    raw = frame(["2026-08-17T03:00Z", "2026-08-22T06:00Z", "2026-08-29T06:00Z", "2026-09-05T06:00Z"])
    storage.merge_and_write("RUS:MX1!", raw, "1W")
    before = storage.path_for("RUS:MX1!", "1W").read_bytes()
    sync = HistorySynchronizer(config, FakeProvider([]), storage)
    service = AssetBarsService(config, sync)
    response = service.get_bars("RUS:MX1!", "1W", "2026-08-31T00:00Z", count=3)
    assert "error" not in response
    assert [b["t"] for b in response["bars"]] == ["2026-08-17T00:00:00Z", "2026-08-24T00:00:00Z", "2026-08-31T00:00:00Z"]
    assert [b["is_bar_complete"] for b in response["bars"]] == [True, True, False]
    kept = [b for b in response["bars"] if pd.Timestamp(b["t"]) + pd.Timedelta(days=7) <= pd.Timestamp("2026-08-31T00:00Z")]
    assert kept[-1]["t"] == "2026-08-24T00:00:00Z"
    assert "timestamp_normalization" not in response
    assert all("timestamp_evidence" in bar for bar in response["bars"])
    assert storage.path_for("RUS:MX1!", "1W").read_bytes() == before


@pytest.mark.parametrize("defect", [None, "missing_bar", "bad_volume", "bad_high", "no_successor"])
def test_holiday_tail_requires_complete_matching_segment(defect):
    daily = frame(["2025-12-30T16:00Z", "2026-01-05T16:00Z"])
    daily.iloc[0, 4] = 300
    four = frame(["2025-12-30T16:00Z", "2025-12-30T20:00Z",
                  "2026-01-05T04:00Z", "2026-01-05T08:00Z",
                  "2026-01-05T12:00Z", "2026-01-05T16:00Z"])
    four.iloc[0, 0] = 20
    if defect == "missing_bar":
        four = four.drop(pd.Timestamp("2026-01-05T08:00Z"))
    elif defect == "bad_volume":
        daily.iloc[0, 4] = 301
    elif defect == "bad_high":
        daily.iloc[0, 1] = 13
    elif defect == "no_successor":
        four = four.iloc[:-1]
    cutoff = pd.Timestamp("2026-01-05T00:00Z")
    result = normalize_daily(daily, four, cutoff)
    assert result.index[0] == pd.Timestamp("2026-01-05" if defect is None else "2025-12-31", tz="UTC")
    if defect is None:
        assert result.iloc[0].timestamp_evidence == "four_hour_holiday_tail_ohlv_matches"
        assert result.iloc[0][daily.columns].tolist() == daily.iloc[0].tolist()
        weekly = normalize_weekly(daily.iloc[:1], daily, four, cutoff)
        assert weekly.index[0] == cutoff
        assert weekly.index[0] + pd.Timedelta(days=7) > cutoff


def holiday_session_frames():
    daily = frame(["2025-11-03T16:00Z", "2025-11-05T16:00Z"])
    four = frame(["2025-11-03T16:00Z", "2025-11-03T20:00Z",
                  "2025-11-05T05:00Z", "2025-11-05T09:00Z",
                  "2025-11-05T13:00Z", "2025-11-05T16:00Z"])
    hourly = frame(["2025-11-05T13:00Z", "2025-11-05T14:00Z",
                    "2025-11-05T15:00Z", "2025-11-05T16:00Z"])
    return daily, four, hourly


def test_shortened_four_hour_session_end_verified_with_hourly_prices():
    daily, four, hourly = holiday_session_frames()
    result = normalize_daily(daily, four, pd.Timestamp("2025-11-10", tz="UTC"), hourly=hourly)
    assert result.index[0] == pd.Timestamp("2025-11-05", tz="UTC")
    assert result.iloc[0].timestamp_evidence == "four_hour_ohl_matches_hourly_session_end"
    assert result[daily.columns].reset_index(drop=True).equals(daily.reset_index(drop=True))
    weekly = normalize_weekly(daily.iloc[:1], daily, four, pd.Timestamp("2025-11-10", tz="UTC"), hourly=hourly)
    assert weekly.iloc[0].timestamp_evidence == "four_hour_ohl_matches_hourly_session_end"


@pytest.mark.parametrize("defect", ["missing_hour", "no_successor_hour", "post_boundary_prices", "no_hourly"])
def test_shortened_session_cannot_use_missing_or_post_boundary_evidence(defect):
    daily, four, hourly = holiday_session_frames()
    if defect == "missing_hour":
        hourly = hourly.drop(pd.Timestamp("2025-11-05T14:00Z"))
    elif defect == "no_successor_hour":
        hourly = hourly.iloc[:-1]
    elif defect == "post_boundary_prices":
        # Even matching daily/4h highs must not admit a spike after the cutoff.
        four.loc[pd.Timestamp("2025-11-05T13:00Z"), "high"] = 99
        daily.iloc[0, daily.columns.get_loc("high")] = 99
        hourly.loc[pd.Timestamp("2025-11-05T16:00Z"), "high"] = 99
    else:
        hourly = None
    result = normalize_daily(daily, four, pd.Timestamp("2025-11-10", tz="UTC"), hourly=hourly)
    assert result.index[0] == pd.Timestamp("2025-11-04", tz="UTC")
    assert result.iloc[0].timestamp_basis == "standard_session_rule"


import pytest as _pytest

@_pytest.fixture(autouse=True)
def existing_partial_cache_without_network_bootstrap(monkeypatch):
    monkeypatch.setattr("tv_history.sync.HistorySynchronizer._initialize", lambda *args: False)

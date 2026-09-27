"""Exercise the installed dependency's get_hist path without network access."""

import datetime
import json
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest
import tvDatafeed.main as upstream

from test_sync import make_settings
from tv_history.provider import TvDatafeedProvider
from tv_history.provider_client import UtcTvDatafeed


def wire_data(values):
    payload = json.dumps({"m": "timescale_update", "p": ["session", {
        "s1": {"s": [{"i": i, "v": value} for i, value in enumerate(values)]}
    }]})
    return f"~m~{len(payload)}~m~{payload}"


@pytest.mark.parametrize("local_offset", [0, 3, -5])
def test_real_get_hist_decodes_utc_without_host_local_conversion(tmp_path, monkeypatch, local_offset):
    class LocalDatetime(datetime.datetime):
        @classmethod
        def fromtimestamp(cls, value, tz=None):
            # Simulate the dependency's local clock on Windows too, without
            # changing the real OS timezone. The UTC decoder must bypass it.
            if tz is None:
                return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).replace(
                    tzinfo=None) + datetime.timedelta(hours=local_offset)
            return datetime.datetime.fromtimestamp(value, tz)

    monkeypatch.setattr(upstream, "datetime", SimpleNamespace(datetime=LocalDatetime))
    # Includes the two occurrences of Istanbul's historical DST fold hour,
    # plus a contemporary timestamp. Their epoch identities must survive.
    expected = pd.to_datetime(["2013-10-27T00:00:00Z", "2013-10-27T01:00:00Z",
                               "2026-09-24T12:00:00Z"])
    payload = wire_data([[stamp.timestamp(), 10, 12, 9, 11, 100] for stamp in expected])
    replies = iter([payload, '{"m":"series_completed"}'])

    def initialize(client, **kwargs):
        client.token = "test-token"
        client.chart_session = "chart"
        client.session = "quote"
        client.ws = SimpleNamespace(recv=lambda: next(replies))

    monkeypatch.setattr(UtcTvDatafeed, "__init__", initialize)
    monkeypatch.setattr(UtcTvDatafeed, "_TvDatafeed__create_connection", lambda self: None)
    monkeypatch.setattr(UtcTvDatafeed, "_TvDatafeed__send_message", lambda *args: None)
    provider = TvDatafeedProvider(replace(make_settings(tmp_path),
                                         provider_naive_timezone="Europe/Istanbul"))
    result = provider.get_history("BITSTAMP:BTCUSD", "1h", 3)
    pd.testing.assert_index_equal(result.index.as_unit("ns"), expected.as_unit("ns").rename("timestamp_utc"))
    assert result.iloc[0].tolist() == [10, 12, 9, 11, 100]


@pytest.mark.parametrize("volume", [[], [None], [0], [123]])
def test_decoder_preserves_prices_and_missing_volume(volume):
    result = UtcTvDatafeed._TvDatafeed__create_df(
        wire_data([[0, 1, 3, 0.5, 2, *volume]]), "TEST")
    assert result.index[0] == pd.Timestamp("1970-01-01", tz="UTC")
    assert result.iloc[0].tolist() == ["TEST", 1, 3, 0.5, 2, 123 if volume == [123] else 0]


@pytest.mark.parametrize("payload", ['{"m":"no_data"}', wire_data([])])
def test_decoder_returns_none_for_no_bars(payload):
    assert UtcTvDatafeed._TvDatafeed__create_df(payload, "TEST") is None

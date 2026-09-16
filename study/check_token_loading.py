"""Validate project token loading without printing credentials or changing caches."""
import logging
import os
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tvDatafeed import Interval
from tv_history.provider import TvDatafeedProvider

logging.disable(logging.CRITICAL)
project = Path(__file__).resolve().parents[1]
expected = (project / "_token.txt").read_text(encoding="utf-8-sig").strip()
assert expected, "Token file is empty"
previous_cwd = Path.cwd()
try:
    os.chdir(project.parent)
    provider = TvDatafeedProvider(None)
    client = provider._get_client()
    if client.token != expected:
        raise RuntimeError("Project token was not loaded")
    assert provider._get_client() is client
    print("Project token loaded from a different working directory; client reused.")
finally:
    os.chdir(previous_cwd)

for exists, contents in [(False, ""), (True, " \n")]:
    with patch("tvDatafeed.TvDatafeed") as factory:
        factory.return_value.token = "login-fallback"
        with patch("tv_history.provider.Path.is_file", return_value=exists):
            with patch("tv_history.provider.Path.read_text", return_value=contents):
                fallback = TvDatafeedProvider(None)._get_client()
        assert fallback.token == "login-fallback"
print("Missing and empty token files preserve login fallback.")

client._TvDatafeed__ws_timeout = 10
try:
    bars = client.get_hist("SI1!", "RUS", Interval.in_1_minute, n_bars=2)
    if bars is None or bars.empty:
        raise RuntimeError("No live bars returned")
    print("Direct provider client check:", datetime.now().isoformat(),
          "last bar:", str(bars.index[-1]), "close:", float(bars.iloc[-1]["close"]))
finally:
    if client.ws:
        client.ws.close()

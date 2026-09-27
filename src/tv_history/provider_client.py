"""Keep TradingView epoch timestamps in UTC at the decoding boundary."""

import json
import re

import pandas as pd
from tvDatafeed import TvDatafeed


class UtcTvDatafeed(TvDatafeed):
    # get_hist calls this name-mangled hook in tvDatafeed. Override only this
    # client's decoder; never patch the dependency or process-wide timezone.
    @staticmethod
    def _TvDatafeed__create_df(raw_data, symbol):
        match = re.search(r'"s"\s*:\s*(?=\[)', raw_data)
        if match is None:
            return None
        bars, _ = json.JSONDecoder().raw_decode(raw_data[match.end():])
        if not bars:
            return None
        rows = []
        timestamps = []
        for bar in bars:
            values = bar["v"]
            timestamps.append(float(values[0]))
            # Instruments without volume use zero, as in tvDatafeed.
            volume = values[5] if len(values) > 5 else None
            rows.append([*(float(value) for value in values[1:5]),
                         0.0 if volume is None else float(volume)])
        frame = pd.DataFrame(
            rows, columns=["open", "high", "low", "close", "volume"],
            index=pd.to_datetime(timestamps, unit="s", utc=True),
        )
        frame.index.name = "datetime"
        frame.insert(0, "symbol", symbol)
        return frame

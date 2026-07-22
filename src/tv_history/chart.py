from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import pandas as pd

from .analysis import parse_timestamp, timeframe_duration
from .config import Settings
from .provider import normalize_asset
from .resample import bar_status, completed_as_of
from .storage import CsvStorage
from .sync import HistorySynchronizer


MAX_DAYS = 365
MAX_BARS = 1500


@dataclass(frozen=True)
class ChartResult:
    metadata: dict
    image_bytes: bytes


class AssetChartService:
    def __init__(self, settings: Settings, synchronizer: HistorySynchronizer, storage: CsvStorage):
        self.settings = settings
        self.synchronizer = synchronizer
        self.storage = storage

    def render(
        self,
        asset: str,
        timeframe: str,
        timestamp: str | None,
        days: int | str,
        response_version: str = "legacy",
    ) -> ChartResult | dict:
        try:
            normalized_asset = normalize_asset(asset)
            requested_at = parse_timestamp(timestamp)
            duration = timeframe_duration(timeframe)
            requested_days = parse_days(days)
            if response_version not in {"legacy", "execution"}:
                raise ValueError("response_version must be legacy or execution")
        except (TypeError, ValueError) as exc:
            return {"error": "invalid_parameter", "message": str(exc)}

        try:
            source, _refresh = self.synchronizer.ensure_available(
                normalized_asset, timeframe, requested_at
            )
            completed = completed_as_of(source, timeframe, requested_at)
            window_start = requested_at - pd.Timedelta(days=requested_days)
            close_times = completed.index + duration
            bars = completed.loc[(close_times > window_start) & (close_times <= requested_at)]

            if bars.empty:
                return {
                    "error": "no_bars_in_window",
                    "asset": normalized_asset,
                    "timeframe": timeframe,
                    "requested_at": requested_at.isoformat(),
                    "requested_days": requested_days,
                }
            if len(bars) > MAX_BARS:
                return {
                    "error": "too_many_bars_to_render",
                    "message": (
                        f"The requested window contains {len(bars)} bars; maximum is {MAX_BARS}. "
                        "Use fewer days or a larger timeframe."
                    ),
                }

            metadata = {
                "asset": normalized_asset,
                "timeframe": timeframe,
                "requested_at": requested_at.isoformat(),
                "effective_bar_close": (bars.index.max() + duration).isoformat(),
                **bar_status(timeframe, bars.index.max(), requested_at),
                "requested_days": requested_days,
                "coverage_from": bars.index.min().isoformat(),
                "coverage_to": (bars.index.max() + duration).isoformat(),
            }
            if response_version == "execution":
                decimals = infer_price_decimals(bars)
                metadata.update(
                    {
                        "reference_prices": {
                            "last_close": display_price(bars.iloc[-1]["close"], decimals),
                            "chart_low": display_price(bars["low"].min(), decimals),
                            "chart_high": display_price(bars["high"].max(), decimals),
                        },
                        "price_decimals": decimals,
                    }
                )
            return ChartResult(metadata=metadata, image_bytes=render_candles(bars, metadata))
        except Exception as exc:
            return {
                "error": "chart_render_failed",
                "message": str(exc),
                "asset": normalized_asset,
            }


def parse_days(value: int | str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"days must be an integer between 1 and {MAX_DAYS}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"days must be an integer between 1 and {MAX_DAYS}") from exc
    if str(value).strip() != str(parsed) or not 1 <= parsed <= MAX_DAYS:
        raise ValueError(f"days must be an integer between 1 and {MAX_DAYS}")
    return parsed


def infer_price_decimals(bars: pd.DataFrame) -> int:
    decimals = 0
    for value in bars[["open", "high", "low", "close"]].to_numpy().ravel():
        text = f"{float(value):.10f}".rstrip("0").rstrip(".")
        if "." in text:
            decimals = max(decimals, len(text.rsplit(".", 1)[1]))
    return decimals


def display_price(value, decimals: int):
    return round(float(value), decimals)


def render_candles(bars: pd.DataFrame, metadata: dict) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import mplfinance as mpf

    plot_frame = bars[["open", "high", "low", "close", "volume"]].copy()
    plot_frame.columns = ["Open", "High", "Low", "Close", "Volume"]
    plot_frame.index = plot_frame.index.tz_convert("UTC").tz_localize(None)

    colors = mpf.make_marketcolors(
        up="#26a69a",
        down="#ef5350",
        edge="inherit",
        wick="inherit",
        volume="inherit",
    )
    style = mpf.make_mpf_style(
        marketcolors=colors,
        facecolor="#ffffff",
        figcolor="#ffffff",
        gridcolor="#d9dde3",
        gridstyle=":",
        rc={
            "axes.labelcolor": "#222222",
            "axes.edgecolor": "#777777",
            "xtick.color": "#333333",
            "ytick.color": "#333333",
            "font.size": 9,
        },
    )
    title = (
        f"{metadata['asset']} · {metadata['timeframe']} · {metadata['requested_days']} days\n"
        f"Completed bars through {metadata['coverage_to']}"
    )

    figure = None
    try:
        figure, axes = mpf.plot(
            plot_frame,
            type="candle",
            style=style,
            volume=True,
            returnfig=True,
            figsize=(14, 8),
            title=title,
            datetime_format="%Y-%m-%d\n%H:%M",
            xrotation=0,
            tight_layout=True,
            warn_too_much_data=MAX_BARS + 1,
        )
        axes[0].set_ylabel("Price")
        axes[2].set_ylabel("Volume")
        axes[2].set_xlabel("UTC")
        buffer = BytesIO()
        figure.savefig(buffer, format="png", dpi=100, bbox_inches="tight")
        return buffer.getvalue()
    finally:
        if figure is not None:
            plt.close(figure)

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import pandas as pd

from .analysis import parse_timestamp, timeframe_duration
from .config import Settings
from .resample import completed_as_of, resample_ohlcv
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
    ) -> ChartResult | dict:
        normalized_asset = asset.strip().upper()
        if normalized_asset not in self.settings.assets:
            return {
                "error": "asset_not_configured",
                "asset": normalized_asset,
                "configured_assets": sorted(self.settings.assets),
            }

        try:
            requested_at = parse_timestamp(timestamp)
            duration = timeframe_duration(timeframe)
            requested_days = parse_days(days)
        except (TypeError, ValueError) as exc:
            return {"error": "invalid_parameter", "message": str(exc)}

        try:
            hourly, refresh = self.synchronizer.ensure_available(normalized_asset, requested_at)
            resampled = resample_ohlcv(hourly, timeframe)
            completed = completed_as_of(resampled, timeframe, requested_at)
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
                    "refresh": refresh,
                }
            if len(bars) > MAX_BARS:
                return {
                    "error": "too_many_bars_to_render",
                    "message": (
                        f"The requested window contains {len(bars)} bars; maximum is {MAX_BARS}. "
                        "Use fewer days or a larger timeframe."
                    ),
                }

            earliest_close = completed.index.min() + duration
            metadata = {
                "asset": normalized_asset,
                "timeframe": timeframe,
                "requested_at": requested_at.isoformat(),
                "requested_from": window_start.isoformat(),
                "requested_days": requested_days,
                "bars_rendered": len(bars),
                "coverage_from": bars.index.min().isoformat(),
                "coverage_to": (bars.index.max() + duration).isoformat(),
                "coverage_complete": earliest_close <= window_start,
                "source": "tvdatafeed_local_csv",
                "refresh": refresh,
            }
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

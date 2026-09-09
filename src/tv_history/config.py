from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


DEFAULT_RUS_DAILY_TRADING_DATE_ASSETS = ("RUS:MX1!", "RUS:SI1!")


@dataclass(frozen=True)
class Settings:
    project_root: Path
    data_root: Path
    provider_naive_timezone: str
    initial_bars: int
    refresh_overlap_bars: int
    indicators: dict
    finalization_delay_minutes: int = 5
    finalization_delay_by_exchange: dict[str, int] | None = None
    rus_daily_trading_date_assets: tuple[str, ...] = DEFAULT_RUS_DAILY_TRADING_DATE_ASSETS
    timestamp_profiles: dict[str, str] | None = None


def load_settings(path: str | Path | None = None) -> Settings:
    project_root = Path(__file__).resolve().parents[2]
    config_path = Path(path or os.environ.get("TV_HISTORY_CONFIG", project_root / "config.yaml"))
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    storage = raw["storage"]
    provider = raw["provider"]
    default_delay = int(provider.get("finalization_delay_minutes", 5))
    delay_by_exchange = {
        str(exchange).upper(): int(minutes)
        for exchange, minutes in provider.get("finalization_delay_by_exchange", {}).items()
    }
    if default_delay < 0 or any(minutes < 0 for minutes in delay_by_exchange.values()):
        raise ValueError("finalization delays must be non-negative minutes")
    # Import after Settings is defined: the provider also consumes Settings.
    from .provider import split_asset

    configured_assets = provider.get(
        "rus_daily_trading_date_assets", list(DEFAULT_RUS_DAILY_TRADING_DATE_ASSETS)
    )
    if not isinstance(configured_assets, list) or any(
        not isinstance(asset, str) for asset in configured_assets
    ):
        raise ValueError("provider.rus_daily_trading_date_assets must be a list of RUS asset strings")
    trading_date_assets = []
    for asset in configured_assets:
        symbol, exchange = split_asset(asset)
        if exchange != "RUS":
            raise ValueError("provider.rus_daily_trading_date_assets supports only RUS assets")
        canonical_asset = f"{exchange}:{symbol}"
        if canonical_asset in trading_date_assets:
            raise ValueError(f"Duplicate daily trading-date asset: {canonical_asset}")
        trading_date_assets.append(canonical_asset)
    data_root = Path(storage["root"])
    profiles = provider.get("timestamp_profiles")
    if profiles is not None:
        if not isinstance(profiles, dict):
            raise ValueError("provider.timestamp_profiles must be an asset-to-profile mapping")
        canonical_profiles = {}
        for asset, profile in profiles.items():
            if not isinstance(asset, str) or profile not in {"moex_futures", "cme_overnight", "ice_brent", "utc_calendar"}:
                raise ValueError("Invalid timestamp asset/profile")
            symbol, exchange = split_asset(asset)
            key = f"{exchange}:{symbol}"
            if key in canonical_profiles:
                raise ValueError(f"Duplicate timestamp asset: {key}")
            canonical_profiles[key] = profile
        profiles = canonical_profiles
    if not data_root.is_absolute():
        data_root = project_root / data_root
    return Settings(
        project_root=project_root,
        data_root=data_root,
        provider_naive_timezone=storage.get("provider_naive_timezone", "UTC"),
        initial_bars=int(provider.get("initial_bars", 5000)),
        refresh_overlap_bars=int(provider.get("refresh_overlap_bars", 10)),
        indicators=dict(raw["indicators"]),
        finalization_delay_minutes=default_delay,
        finalization_delay_by_exchange=delay_by_exchange,
        rus_daily_trading_date_assets=tuple(trading_date_assets),
        timestamp_profiles=profiles,
    )

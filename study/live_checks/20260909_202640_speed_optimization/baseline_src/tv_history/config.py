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
    finalization_delay_by_asset: dict[str, int] | None = None
    calendar_sessions: int = 30
    calendar_lookback_days: int = 90
    calendar_min_observations: int = 4
    calendar_min_agreement: float = 0.9
    calendar_timezones: dict[str, str] | None = None
    calendar_boundary_overrides: dict | None = None
    calendar_timezones_by_exchange: dict[str, str] | None = None
    market_rules: dict | None = None


def load_settings(path: str | Path | None = None) -> Settings:
    project_root = Path(__file__).resolve().parents[2]
    config_path = Path(path or os.environ.get("TV_HISTORY_CONFIG", project_root / "config.yaml"))
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    storage = raw["storage"]
    provider = raw["provider"]
    market_rules = None
    if "market_rules" in raw:
        from .market_rules import parse_market_rules
        if ({"timestamp_profiles", "rus_daily_trading_date_assets"} & provider.keys()
                or {"timezones", "timezones_by_exchange"} & raw.get("calendar", {}).keys()):
            raise ValueError("market_rules cannot be combined with legacy profile/timezone settings")
        market_rules = parse_market_rules(raw["market_rules"])
    default_delay = int(provider.get("finalization_delay_minutes", 5))
    delay_by_exchange = {
        str(exchange).upper(): int(minutes)
        for exchange, minutes in provider.get("finalization_delay_by_exchange", {}).items()
    }
    if default_delay < 0 or any(minutes < 0 for minutes in delay_by_exchange.values()):
        raise ValueError("finalization delays must be non-negative minutes")
    # Import after Settings is defined: the provider also consumes Settings.
    from .provider import split_asset

    def asset_mapping(values):
        result = {}
        for asset, value in values.items():
            symbol, exchange = split_asset(asset)
            key = f"{exchange}:{symbol}"
            if key in result:
                raise ValueError(f"Duplicate canonical asset: {key}")
            result[key] = value
        return result

    asset_delays = {key: int(value) for key, value in asset_mapping(
        provider.get("finalization_delay_by_asset", {})
    ).items()}
    if any(value < 0 for value in asset_delays.values()):
        raise ValueError("finalization delays must be non-negative minutes")
    calendar = raw.get("calendar", {})
    sessions = int(calendar.get("completed_sessions", 30))
    lookback = int(calendar.get("lookback_days", 90))
    observations = int(calendar.get("min_observations", 4))
    agreement = float(calendar.get("min_agreement", 0.9))
    if min(sessions, lookback, observations) < 1 or not 0 < agreement <= 1:
        raise ValueError("Invalid calendar evidence settings")
    timezones = asset_mapping(calendar.get("timezones", {}))
    exchange_timezones = {}
    for exchange, zone in calendar.get("timezones_by_exchange", {}).items():
        key = str(exchange).strip().upper()
        if not key or ":" in key:
            raise ValueError("calendar.timezones_by_exchange requires exchange names")
        if key in exchange_timezones:
            raise ValueError(f"Duplicate calendar timezone exchange: {key}")
        exchange_timezones[key] = zone
    from zoneinfo import ZoneInfo
    for zone in (*timezones.values(), *exchange_timezones.values()):
        ZoneInfo(zone)
    boundary_overrides = asset_mapping(calendar.get("boundary_overrides", {}))
    import pandas as pd
    normalized_boundaries = {}
    for asset, timeframes in boundary_overrides.items():
        normalized_boundaries[asset] = {}
        for timeframe, entries in timeframes.items():
            if timeframe not in {"1h", "4h", "1D", "1W"}:
                raise ValueError("Unsupported boundary override timeframe")
            normalized_boundaries[asset][timeframe] = {}
            for opened, closed in entries.items():
                start, end = pd.Timestamp(opened), pd.Timestamp(closed)
                if start.tzinfo is None or end.tzinfo is None or end <= start:
                    raise ValueError("Boundary override must have aware timestamps and close after open")
                key = start.tz_convert("UTC").isoformat()
                if key in normalized_boundaries[asset][timeframe]:
                    raise ValueError("Duplicate boundary override opening")
                normalized_boundaries[asset][timeframe][key] = end.tz_convert("UTC").isoformat()
    if int(provider.get("initial_bars", 5000)) < 1:
        raise ValueError("initial_bars must be positive")

    configured_assets = provider.get(
        "rus_daily_trading_date_assets", [] if market_rules is not None else list(DEFAULT_RUS_DAILY_TRADING_DATE_ASSETS)
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
        finalization_delay_by_asset=asset_delays,
        calendar_sessions=sessions,
        calendar_lookback_days=lookback,
        calendar_min_observations=observations,
        calendar_min_agreement=agreement,
        calendar_timezones=timezones,
        calendar_boundary_overrides=normalized_boundaries,
        calendar_timezones_by_exchange=exchange_timezones,
        market_rules=market_rules,
    )

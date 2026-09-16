"""Resolve exchange defaults and explicit symbol exceptions in one place."""
from dataclasses import dataclass
from zoneinfo import ZoneInfo


# Compatibility for configurations and callers predating market_rules.
LEGACY_PROFILE_TIMEZONES = {
    "moex_futures": "Europe/Moscow",
    "cme_overnight": "America/Chicago",
    "ice_brent": "Europe/London",
    "utc_calendar": "UTC",
}


@dataclass(frozen=True)
class MarketRule:
    timezone: str | None = None
    timestamp_profile: str | None = None


def parse_market_rules(raw):
    if not isinstance(raw, dict) or set(raw) != {"exchanges"}:
        raise ValueError("market_rules must contain an exchanges mapping")
    if not isinstance(raw["exchanges"], dict):
        raise ValueError("market_rules.exchanges must be a mapping")

    def properties(value, allowed):
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError("Invalid market rule fields")
        if "timezone" in value:
            if not isinstance(value["timezone"], str):
                raise ValueError("Market rule timezone must be a timezone name")
            ZoneInfo(value["timezone"])
        if "timestamp_profile" in value:
            profile = value["timestamp_profile"]
            if profile is not None and (not isinstance(profile, str) or profile not in LEGACY_PROFILE_TIMEZONES):
                raise ValueError("Invalid market rule timestamp_profile")

    def validate_resolved(value):
        if not value.get("timezone"):
            raise ValueError("Market rules require an exchange or symbol timezone")
        if value.get("timestamp_profile") == "utc_calendar" and value["timezone"] != "UTC":
            raise ValueError("utc_calendar requires timezone UTC")

    result = {}
    for exchange, values in raw["exchanges"].items():
        if not isinstance(exchange, str) or not exchange.strip() or ":" in exchange:
            raise ValueError("Market rules require exchange names")
        exchange = exchange.strip().upper()
        if exchange in result:
            raise ValueError(f"Duplicate market rule exchange: {exchange}")
        properties(values, {"timezone", "timestamp_profile", "symbols"})
        defaults = {k: v for k, v in values.items() if k != "symbols"}
        validate_resolved(defaults)
        symbols = values.get("symbols", {})
        if not isinstance(symbols, dict):
            raise ValueError("Market rule symbols must be a mapping")
        overrides = {}
        for symbol, values in symbols.items():
            if not isinstance(symbol, str) or not symbol.strip() or ":" in symbol:
                raise ValueError("Market rule symbols must be bare ticker names")
            symbol = symbol.strip().upper()
            if symbol in overrides:
                raise ValueError(f"Duplicate market rule symbol: {exchange}:{symbol}")
            properties(values, {"timezone", "timestamp_profile"})
            validate_resolved({**defaults, **values})
            overrides[symbol] = dict(values)
        result[exchange] = {**defaults, "symbols": overrides}
    return result


def resolve_market_rule(settings, asset):
    from .provider import split_asset
    symbol, exchange = split_asset(asset)
    asset = f"{exchange}:{symbol}"
    if settings.market_rules is not None:
        defaults = settings.market_rules.get(exchange, {})
        override = defaults.get("symbols", {}).get(symbol, {})
        # Explicit null clears the inherited profile; absence inherits it.
        resolved = {**defaults, **override}
        return MarketRule(resolved.get("timezone"), resolved.get("timestamp_profile"))

    profile = (settings.timestamp_profiles or {}).get(asset)
    if profile is None and asset in settings.rus_daily_trading_date_assets:
        profile = "moex_futures"
    zone = ((settings.calendar_timezones or {}).get(asset)
            or LEGACY_PROFILE_TIMEZONES.get(profile)
            or (settings.calendar_timezones_by_exchange or {}).get(exchange))
    return MarketRule(zone, profile)

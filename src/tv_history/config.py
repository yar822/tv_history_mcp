from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


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
    data_root = Path(storage["root"])
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
    )

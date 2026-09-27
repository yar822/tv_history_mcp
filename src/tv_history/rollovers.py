"""One startup-only, source-reported rollover calendar per continuous asset."""
from copy import deepcopy
from threading import RLock
import logging

import pandas as pd

from .provider import split_asset
from .provider_rollovers import RolloverError, FUTURE_TARGET

logger = logging.getLogger(__name__)


def local_timestamps(document):
    """Upgrade stored events as well as fresh ones, without changing UTC."""
    for event in document.get("events", []):
        event["scheduled_at_local"] = pd.Timestamp(event["scheduled_at_utc"]).tz_convert(document["timezone"]).isoformat()
        event.setdefault("timestamp_basis", "1h")
    return document


def merge_snapshot(document, snapshot, now):
    """Retain older events when the source returns a shorter window."""
    result = deepcopy(document)
    stamp = now.isoformat()
    events = {(e["from"], e["to"]): e for e in result.get("events", [])}
    for fresh in snapshot["events"]:
        key = (fresh["from"], fresh["to"])
        previous = events.get(key, {})
        event = {**fresh, "first_seen_at_utc": previous.get("first_seen_at_utc", stamp),
                 "last_seen_at_utc": stamp}
        events[key] = event
    available = sum(pd.Timestamp(e["scheduled_at_utc"]) > now for e in snapshot["events"])
    result.update(schema_version=2, timezone=snapshot["timezone"], source=snapshot["source"],
                  checked_at_utc=stamp, last_attempt_at_utc=stamp,
                  future_target=FUTURE_TARGET, future_available=available,
                  future_status="ready" if available >= FUTURE_TARGET else "limited",
                  last_result="events" if snapshot["events"] else "empty",
                  events=sorted(events.values(), key=lambda e: (e["scheduled_at_utc"], e["from"])))
    result.pop("last_error", None)
    return local_timestamps(result)


class RolloverCalendars:
    def __init__(self, provider, storage):
        self.provider, self.storage = provider, storage
        self._lock = RLock()
        self._attempted = set()

    def refresh(self, asset, now):
        """Attempt each asset once per startup, regardless of saved freshness."""
        symbol, exchange = split_asset(asset)
        getter = getattr(self.provider, "get_rollovers", None)
        if not symbol.endswith("1!") or getter is None:
            return
        asset = f"{exchange}:{symbol}"
        with self._lock:
            if asset in self._attempted:
                return
            self._attempted.add(asset)
            try:
                document = self.storage.read_rollovers(asset)
                try:
                    snapshot = getter(asset)
                    updated = merge_snapshot(document, snapshot, now)
                except Exception as exc:
                    updated = deepcopy(document)
                    updated.update(schema_version=2, last_attempt_at_utc=now.isoformat(),
                                   last_error=str(exc) if isinstance(exc, RolloverError) else "source_unavailable")
                    local_timestamps(updated)
                self.storage.save_rollovers(asset, updated)
            except Exception:
                # Storage errors are non-fatal too; never log raw exceptions.
                logger.warning("Could not persist futures rollover metadata for %s", asset)

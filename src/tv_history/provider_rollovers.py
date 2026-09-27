"""Read TradingView's separate roll-date study without persisting price bars."""
from datetime import datetime, timezone
import json
import math
import time
import uuid
from zoneinfo import ZoneInfo

from websocket import create_connection

STUDY = "BarSetContinuousRollDates@tv-corestudies"
MONTH_CODES = "FGHJKMNQUVXZ"
FUTURE_TARGET = 3


class RolloverError(Exception):
    """Only fixed, non-sensitive error codes may leave this adapter."""


def decode_events(rows, plots, exchange, root):
    positions = {plot["id"]: i + 1 for i, plot in enumerate(plots)}
    required = [name + suffix for suffix in ("", "2", "3", "4")
                for name in ("CurrentContractCode", "NextContractCode", "SwitchDate")]
    if any(name not in positions for name in required) or "Overflowed" not in positions:
        raise RolloverError("unsupported_study_schema")
    events = {}

    def integer(value):
        if type(value) not in (int, float) or not math.isfinite(value) or value != int(value):
            raise RolloverError("invalid_event")
        return int(value)

    def contract(value):
        year, month = divmod(integer(value), 100)
        if not 1900 <= year <= 2200 or not 1 <= month <= 12:
            raise RolloverError("invalid_contract")
        return f"{exchange}:{root}{MONTH_CODES[month - 1]}{year}"

    for row in rows:
        values = row["v"]
        if len(values) <= max(positions.values()):
            raise RolloverError("invalid_event")
        if values[positions["Overflowed"]] != 0:
            raise RolloverError("event_overflow")
        for suffix in ("", "2", "3", "4"):
            old, new, date = [values[positions[name + suffix]] for name in
                              ("CurrentContractCode", "NextContractCode", "SwitchDate")]
            # The study's unused slots use 1e100 (or null), not contract codes.
            missing = [v is None or (type(v) in (int, float) and v >= 1e99)
                       for v in (old, new, date)]
            if all(missing):
                continue
            if any(missing):
                raise RolloverError("incomplete_event")
            event = {
                "from": contract(old), "to": contract(new),
                "trading_date": datetime.strptime(str(integer(date)), "%Y%m%d").date().isoformat(),
                "scheduled_at_utc": datetime.fromtimestamp(integer(values[0]), timezone.utc).isoformat(),
            }
            key = (event["from"], event["to"])
            if key[0] == key[1] or (key in events and events[key] != event):
                raise RolloverError("conflicting_event")
            events[key] = event
    return sorted(events.values(), key=lambda e: (e["scheduled_at_utc"], e["from"]))


def combine_calendars(hourly, daily, now):
    """Hourly anchors win; daily anchors extend the published future horizon.

    A daily anchor is not proof of the eventual first hourly switch bar.
    Historical daily anchors must never replace the verified hourly history.
    """
    events = {(e["from"], e["to"]): {**e, "timestamp_basis": "1h"} for e in hourly}
    future = sorted((e for e in daily if datetime.fromisoformat(e["scheduled_at_utc"]) > now),
                    key=lambda e: e["scheduled_at_utc"])[:FUTURE_TARGET]
    for event in future:
        events.setdefault((event["from"], event["to"]), {**event, "timestamp_basis": "1D"})
    return sorted(events.values(), key=lambda e: (e["scheduled_at_utc"], e["from"]))


def collect_rollovers(asset, token, timeout=20):
    # TradingView permits only one supporting series in a chart session.
    deadline = time.monotonic() + timeout
    hourly = _collect_calendar(asset, token, "60", timeout)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RolloverError("timeout")
    daily = _collect_calendar(asset, token, "1D", remaining)
    if (hourly["source"], hourly["timezone"]) != (daily["source"], daily["timezone"]):
        raise RolloverError("inconsistent_calendars")
    return {**hourly, "events": combine_calendars(hourly["events"], daily["events"], datetime.now(timezone.utc))}


def _collect_calendar(asset, token, interval, timeout):
    """Return a completed source snapshot, or raise a sanitized failure.

    Study rows outside the supporting series (negative/future indices) carry
    historical/upcoming events and must not be clipped to the OHLC window.
    """
    ws = None
    try:
        exchange, symbol = asset.split(":", 1)
        root = symbol[:-2]
        deadline = time.monotonic() + timeout
        ws = create_connection("wss://data.tradingview.com/socket.io/websocket",
                               origin="https://data.tradingview.com", timeout=timeout)
        session = "cs_" + uuid.uuid4().hex[:12]

        def send(method, params):
            payload = json.dumps({"m": method, "p": params}, separators=(",", ":"))
            ws.send(f"~m~{len(payload)}~m~{payload}")

        send("set_auth_token", [token or "unauthorized_user_token"])
        send("chart_create_session", [session, ""])
        send("resolve_symbol", [session, "symbol_1", "=" + json.dumps(
            {"symbol": asset, "adjustment": "splits", "session": "regular"})])
        send("request_studies_metadata", [""])
        buffer, spec, metadata, started = "", None, None, False
        rows = {}
        while time.monotonic() < deadline:
            ws.settimeout(max(0.01, deadline - time.monotonic()))
            chunk = ws.recv()
            if not chunk:
                raise RolloverError("connection_closed")
            buffer += chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            while buffer:
                if len(buffer) < 3 and "~m~".startswith(buffer):
                    break
                if not buffer.startswith("~m~"):
                    raise RolloverError("invalid_frame")
                end = buffer.find("~m~", 3)
                if end < 0:
                    break
                length = int(buffer[3:end])
                if len(buffer) < end + 3 + length:
                    break
                payload, buffer = buffer[end + 3:end + 3 + length], buffer[end + 3 + length:]
                if payload.startswith("~h~"):
                    ws.send(f"~m~{len(payload)}~m~{payload}")
                    continue
                message = json.loads(payload)
                kind, params = message.get("m"), message.get("p", [])
                if kind in ("protocol_error", "critical_error"):
                    raise RolloverError("source_error")
                if kind == "studies_metadata":
                    specs = params[1]["metainfo"]
                    candidates = [s for s in specs if s.get("id", "").startswith(STUDY + "-")]
                    if len(candidates) != 1:
                        raise RolloverError("study_unavailable")
                    spec = candidates[0]
                if params and params[0] == session:
                    if kind == "symbol_resolved" and params[1] == "symbol_1":
                        metadata = params[2]
                        if (metadata.get("type") != "futures" or metadata.get("continuous_order") != 1
                                or metadata.get("root") != root):
                            raise RolloverError("unsupported_symbol")
                        ZoneInfo(metadata["timezone"])
                    if kind in ("symbol_error", "series_error", "study_error"):
                        raise RolloverError(kind)
                    if kind in ("timescale_update", "du"):
                        for row in params[1].get("rolls", {}).get("st", []):
                            rows[row["i"]] = row
                    if kind == "study_completed" and params[1] == "rolls" and started:
                        return {"source": spec["id"], "timezone": metadata["timezone"],
                                "events": decode_events(rows.values(), spec["plots"], exchange, root)}
                if metadata is not None and spec is not None and not started:
                    # Only anchors the study. Supporting bars are discarded.
                    send("create_series", [session, "s1", "s1", "symbol_1", interval, 100])
                    send("create_study", [session, "rolls", "st1", "s1", spec["id"], {"currenttime": "now"}])
                    started = True
        raise RolloverError("timeout")
    except RolloverError:
        raise
    except Exception:
        # Raw websocket/provider errors can contain credentials or auth payloads.
        raise RolloverError("source_unavailable") from None
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

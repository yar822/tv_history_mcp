"""Separate symbol lookup; never requests or changes OHLCV bars."""
import json
import time
import uuid

from websocket import create_connection


def unknown_metadata():
    return {"resolved_symbol": None, "delay_seconds": None, "auth_token": None}


def collect_metadata(asset, token, timeout=5):
    result = unknown_metadata()
    authenticated = bool(token and token != "unauthorized_user_token")
    if not authenticated:
        result["auth_token"] = False
    ws = None
    try:
        deadline = time.monotonic() + timeout
        ws = create_connection("wss://data.tradingview.com/socket.io/websocket",
                               origin="https://data.tradingview.com", timeout=timeout)
        session = "cs_" + uuid.uuid4().hex[:12]
        for method, params in (
            ("set_auth_token", [token or "unauthorized_user_token"]),
            ("chart_create_session", [session, ""]),
            ("resolve_symbol", [session, "symbol_1", asset]),
        ):
            payload = json.dumps({"m": method, "p": params}, separators=(",", ":"))
            ws.send(f"~m~{len(payload)}~m~{payload}")
        while time.monotonic() < deadline:
            ws.settimeout(max(0.01, deadline - time.monotonic()))
            raw = ws.recv()
            if not raw:
                break
            for part in raw.split("~m~"):
                if part.startswith("~h~"):
                    ws.send(f"~m~{len(part)}~m~{part}")
                    continue
                try:
                    message = json.loads(part)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                kind, params = message.get("m"), message.get("p", [])
                if kind in ("protocol_error", "critical_error"):
                    if any(isinstance(p, str) and "auth" in p.lower() for p in params):
                        result["auth_token"] = False
                    return result
                if kind == "symbol_resolved" and len(params) >= 3 and params[:2] == [session, "symbol_1"]:
                    values = params[2]
                    delay = values.get("delay")
                    result.update(
                        resolved_symbol=values.get("full_name"),
                        delay_seconds=delay if type(delay) in (int, float) and delay >= 0 else None,
                        # Ordered symbol resolution succeeds after set_auth_token;
                        # invalid tokens instead produce "bad auth token" and close.
                        auth_token=authenticated,
                    )
                    return result
                if kind == "symbol_error":
                    return result
    except Exception:
        # Optional metadata must not interrupt normal bar retrieval. Never log
        # raw errors or messages, which can contain authentication payloads.
        pass
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
    return result

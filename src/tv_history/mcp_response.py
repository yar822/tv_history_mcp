from __future__ import annotations

import json

from mcp.server.fastmcp import Image
from mcp.types import CallToolResult, TextContent


_INTERNAL_METADATA = {
    "timestamp_normalization",
    "daily_timestamp_normalization",
    "history_coverage",
    "daily_history_coverage",
}


def mcp_result(payload: dict, image_bytes: bytes | None = None) -> CallToolResult:
    # Project the public response without mutating service or cached metadata.
    payload = {key: value for key, value in payload.items() if key not in _INTERNAL_METADATA}
    if isinstance(payload.get("error"), dict):
        payload["error"] = {
            key: value for key, value in payload["error"].items()
            if key not in _INTERNAL_METADATA
        }
    content = [TextContent(type="text", text=json.dumps(payload, indent=2))]
    if image_bytes is not None:
        content.append(Image(data=image_bytes, format="png").to_image_content())
    return CallToolResult(
        content=content,
        structuredContent=payload,
        isError="error" in payload,
    )

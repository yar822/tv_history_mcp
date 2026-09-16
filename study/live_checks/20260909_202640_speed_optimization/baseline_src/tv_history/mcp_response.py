from __future__ import annotations

import json

from mcp.server.fastmcp import Image
from mcp.types import CallToolResult, TextContent


def mcp_result(payload: dict, image_bytes: bytes | None = None) -> CallToolResult:
    content = [TextContent(type="text", text=json.dumps(payload, indent=2))]
    if image_bytes is not None:
        content.append(Image(data=image_bytes, format="png").to_image_content())
    return CallToolResult(
        content=content,
        structuredContent=payload,
        isError="error" in payload,
    )

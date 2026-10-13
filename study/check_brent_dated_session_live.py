"""Inspect dated and recurring Brent sessions through the live MCP."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'calls': []}
    async with streamablehttp_client('http://127.0.0.1:8010/mcp', timeout=30, sse_read_timeout=30) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            report['input_schema'] = next(tool.inputSchema for tool in (await client.list_tools()).tools
                                          if tool.name == 'trading_session')
            for arguments in (
                {'asset': 'ICEEUR:BRN1!', 'session': 'week', 'date': '2025-10-27'},
                {'asset': 'ICEEUR:BRN1!', 'session': 'week', 'date': '2025-11-03'},
                {'asset': 'ICEEUR:BRN1!', 'session': 'week'},
            ):
                response = await client.call_tool('trading_session', arguments)
                payload = response.structuredContent or json.loads(next(c.text for c in response.content if c.type == 'text'))
                report['calls'].append({'arguments': arguments, 'isError': response.isError, 'response': payload})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    asyncio.run(main())

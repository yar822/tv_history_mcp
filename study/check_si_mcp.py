"""Request current SI hourly bars from the running MCP and record fetch evidence."""
import asyncio
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from tv_history.config import load_settings
from tv_history.storage import CsvStorage

cfg = load_settings()
storage = CsvStorage(cfg)
asset = 'RUS:SI1!'

async def main():
    before = storage.read_coverage(asset).get('1h', {}).get('last_download')
    started = datetime.now(timezone.utc)
    async with streamablehttp_client('http://127.0.0.1:8010/mcp') as (read, write, _):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
            await session.initialize()
            reply = await session.call_tool('asset_bars', {'asset':asset, 'timeframe':'1h', 'count':3})
            result = reply.structuredContent or json.loads('\n'.join(c.text for c in reply.content if c.type == 'text'))
    after = storage.read_coverage(asset).get('1h', {}).get('last_download')
    report = {'started_utc':started.isoformat(), 'finished_utc':datetime.now(timezone.utc).isoformat(),
              'new_download_recorded':before != after, 'last_download':after, 'response':result}
    output = cfg.data_root / 'live_checks' / (started.strftime('%Y%m%d_%H%M%S')+'_si_mcp.json')
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))
    print('Saved:',output)

asyncio.run(main())

"""Short sequential MCP/direct SI comparison; no credentials in output."""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from tvDatafeed import Interval
from tv_history.provider import TvDatafeedProvider
from tv_history.config import load_settings
from tv_history.storage import CsvStorage

logging.disable(logging.CRITICAL)
cfg = load_settings()
storage = CsvStorage(cfg)
client = TvDatafeedProvider(cfg)._get_client()
client._TvDatafeed__ws_timeout = 8
records = []

def stamp():
    return datetime.now(timezone.utc).isoformat()

def direct():
    start = stamp()
    try:
        bars = client.get_hist('SI1!', 'RUS', Interval.in_1_hour, n_bars=2)
        if bars is None or bars.empty:
            return {'error':'No bars', 'started_utc':start}
        return {'started_utc':start,'received_utc':stamp(),
                'bar_open_local':str(bars.index[-1]),'close':float(bars.iloc[-1]['close'])}
    except Exception as exc:
        return {'error':type(exc).__name__,'started_utc':start}
    finally:
        if client.ws:
            client.ws.close()

async def main():
    output = cfg.data_root/'live_checks'/(datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')+'_si_comparison.json')
    output.parent.mkdir(parents=True,exist_ok=True)
    async with streamablehttp_client('http://127.0.0.1:8010/mcp') as (read,write,_):
        async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=30)) as session:
            await session.initialize()
            for i in range(7):
                began = time.monotonic()
                before = storage.read_coverage('RUS:SI1!').get('1h',{}).get('last_download')
                started = stamp()
                reply = await session.call_tool('asset_bars',{'asset':'RUS:SI1!','timeframe':'1h','count':2})
                received = stamp()
                result = reply.structuredContent or json.loads('\n'.join(c.text for c in reply.content if c.type=='text'))
                after = storage.read_coverage('RUS:SI1!').get('1h',{}).get('last_download')
                latest = result.get('bars',[{}])[-1]
                record = {'sample':i+1,'mcp':{'started_utc':started,'received_utc':received,
                    'bar_open_utc':latest.get('t'),'close':latest.get('c'),
                    'new_download_recorded':before!=after,
                    'last_fetch_started_utc':(after or {}).get('request_started_at'),
                    'provider_metadata':result.get('provider_metadata'),
                    'error':result.get('error')},'direct':await asyncio.to_thread(direct)}
                records.append(record)
                output.write_text(json.dumps(records,indent=2),encoding='utf-8')
                print(json.dumps(record),flush=True)
                if i<6:
                    await asyncio.sleep(max(0,5-(time.monotonic()-began)))
    print('Saved:',output)

asyncio.run(main())

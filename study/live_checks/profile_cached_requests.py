import asyncio, json, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from tv_history.config import load_settings
cfg=load_settings()
out=cfg.data_root/'live_checks'/datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_cached_latency')
out.mkdir(parents=True)
log=cfg.data_root/'download_control.csv'
records=[]; batches=[]
def save():
    (out/'live.json').write_text(json.dumps({'records':records,'batches':batches},indent=2))
async def main():
    print('OUTPUT',out,flush=True)
    async with streamablehttp_client('http://127.0.0.1:8010/mcp') as (r,w,_):
        async with ClientSession(r,w,read_timeout_seconds=timedelta(seconds=240)) as session:
            await session.initialize()
            async def call(tf,tag,count=100):
                started=time.perf_counter()
                args={'asset':'CME_MINI:NQ1!','timeframe':tf,'timestamp':'2026-09-04T12:00:00Z','count':count}
                response=await session.call_tool('asset_bars',args)
                elapsed=time.perf_counter()-started
                value=response.structuredContent or json.loads('\n'.join(c.text for c in response.content if c.type=='text'))
                rec={'tag':tag,'timeframe':tf,'count':count,'seconds':round(elapsed,3),'error':value.get('error'),'bars_returned':value.get('bars_returned'),'coverage':value.get('history_coverage',{}).get('status')}
                records.append(rec); save(); print(json.dumps(rec),flush=True)
                return rec
            for repeat in range(2):
                for tf in ('1h','1D','1W'):
                    before=log.read_bytes(); rec=await call(tf,'sequential_'+str(repeat))
                    rec['download_log_unchanged']=before==log.read_bytes(); save()
            for n in (2,22):
                before=log.read_bytes(); t=time.perf_counter()
                await asyncio.gather(*(call('1D' if i%2==0 else '1W',f'concurrent_{n}_{i}') for i in range(n)))
                batch={'requests':n,'seconds':round(time.perf_counter()-t,3),'download_log_unchanged':before==log.read_bytes()}
                batches.append(batch); save(); print('BATCH',json.dumps(batch),flush=True)
    print('RESULT',out,flush=True)
asyncio.run(main())

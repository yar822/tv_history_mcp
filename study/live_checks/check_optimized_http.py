import asyncio,json,os,shutil,socket,subprocess,sys,time
from datetime import timedelta
from pathlib import Path
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
out=Path(Path('data/live_checks/active_speed_check.txt').read_text())
cache=out/'http_cache'; shutil.copytree(out/'snapshot',cache)
with socket.socket() as probe:
    probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
expected={r['tf']:r['response'] for r in json.loads((out/'before.json').read_text())['results'] if r['tool']=='bars' and r['asset']=='CME_MINI:NQ1!' and r['count']==100 and r['sessions'] is None}
records=[]; batches=[]
def save(): (out/'optimized_http.json').write_text(json.dumps({'port':port,'records':records,'batches':batches},indent=2))
async def main():
    for _ in range(60):
        try:
            r,w=await asyncio.open_connection('127.0.0.1',port); w.close(); await w.wait_closed(); break
        except OSError:
            if process.poll() is not None: raise RuntimeError('test server exited')
            await asyncio.sleep(0.5)
    else: raise RuntimeError('test server startup timeout')
    async with streamablehttp_client(f'http://127.0.0.1:{port}/mcp') as (r,w,_):
        async with ClientSession(r,w,read_timeout_seconds=timedelta(seconds=180)) as session:
            await session.initialize()
            async def call(tf,tag):
                started=time.perf_counter()
                result=await session.call_tool('asset_bars',{'asset':'CME_MINI:NQ1!','timeframe':tf,'timestamp':'2026-09-04T12:00:00Z','count':100})
                elapsed=time.perf_counter()-started
                value=result.structuredContent or json.loads('\n'.join(c.text for c in result.content if c.type=='text'))
                rec={'tag':tag,'timeframe':tf,'seconds':round(elapsed,3),'exact_baseline_match':value==expected[tf]}
                records.append(rec); save(); print(json.dumps(rec),flush=True)
                assert rec['exact_baseline_match'],rec
            for repeat in range(2):
                for tf in ('1h','1D','1W'): await call(tf,'sequential_'+str(repeat))
            for n in (2,22):
                started=time.perf_counter()
                await asyncio.gather(*(call('1D' if i%2==0 else '1W',f'concurrent_{n}_{i}') for i in range(n)))
                batches.append({'requests':n,'seconds':round(time.perf_counter()-started,3)}); save(); print('BATCH',batches[-1],flush=True)
    assert not (cache/'unexpected_provider_calls.jsonl').exists()
with (out/'http_server.log').open('w') as log:
    process=subprocess.Popen([sys.executable,'study/live_checks/frozen_speed_server.py',str(cache),str(port)],stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try: asyncio.run(main())
    finally:
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=10)
        (out/'http_server_cleanup.json').write_text(json.dumps({'pid':process.pid,'port':port,'exited':process.poll() is not None}))
print('DONE',out,flush=True)

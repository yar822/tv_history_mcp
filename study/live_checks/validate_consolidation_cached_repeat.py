import asyncio,json,time
from pathlib import Path
from datetime import timedelta
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from tv_history.config import load_settings
cfg=load_settings();root=cfg.data_root;out=root/'live_checks/20260909_211949_consolidation_live'; records=[]
async def main():
 before=(root/'download_control.csv').read_bytes()
 async with streamablehttp_client('http://127.0.0.1:8010/mcp') as (r,w,_):
  async with ClientSession(r,w,read_timeout_seconds=timedelta(seconds=180)) as s:
   await s.initialize()
   for asset,tfs in json.loads((root/'ticker_registry.json').read_text()).items():
    for tf in tfs:
     start=time.monotonic();v=await s.call_tool('asset_bars',{'asset':asset,'timeframe':tf,'count':5,'timestamp':'2026-08-30T00:00:00Z'})
     value=v.structuredContent or json.loads('\n'.join(c.text for c in v.content if c.type=='text'))
     assert not v.isError and value.get('bars'),value
     records.append({'asset':asset,'timeframe':tf,'seconds':round(time.monotonic()-start,3)})
 assert before==(root/'download_control.csv').read_bytes(),'provider download on cached repeat'
 (out/'cached_repeat.json').write_text(json.dumps(records,indent=2))
 summary=json.loads((out/'summary.json').read_text());summary['test_correction']='Initial September 8 cutoff exceeded latest weekly raw opening and legitimately triggered eight weekly refreshes under existing logic. August 30 repeat checks the fully cached path.'
 summary['initial_test_assumption_failures']=summary.pop('failures');summary['failures']=[];summary['cached_repeat']=records
 (out/'summary.json').write_text(json.dumps(summary,indent=2))
 print('PASS',len(records),'cached requests, no downloads',min(x['seconds'] for x in records),max(x['seconds'] for x in records));print(summary['audit']['backup_comparison'])
asyncio.run(main())

import asyncio,json,time,shutil
from datetime import datetime,timezone,timedelta
from dataclasses import replace
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from tv_history.config import load_settings
from tv_history.storage import CsvStorage,read_json
from tv_history.sync import HistorySynchronizer
from tv_history.finality import finalization_delay
import pandas as pd
cfg=load_settings();root=cfg.data_root;store=CsvStorage(cfg);assets=json.loads((root/'ticker_registry.json').read_text());out=root/'live_checks'/datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_simplified_live');out.mkdir(parents=True)
summary={'assets':{},'requests':[]};print('OUTPUT',out,flush=True)
for asset,tfs in assets.items():
 folder=store.path_for(asset).parent;cal=read_json(folder/'calendar.json',{});cov=read_json(folder/'coverage.json',{})
 assert cal['schema_version']==2 and cov['metadata_schema']==2
 assert 'rules' not in cal and 'session_templates' not in cal
 assert 'result' not in cov.get('startup_check',{})
 assert all('attempted_gaps' not in cov.get(tf,{}) for tf in tfs)
 assert not list(folder.glob('*.receipts.json')) and not list(folder.glob('*.revisions.jsonl'))
 summary['assets'][asset]={'calendar_bytes':(folder/'calendar.json').stat().st_size,'coverage_bytes':(folder/'coverage.json').stat().st_size,'receipts':sum(len(v) for v in cov['receipt_evidence'].values())}
 dest=out/'restart_copy'/folder.relative_to(root);shutil.copytree(folder,dest)
shutil.copy2(root/'ticker_registry.json',out/'restart_copy/ticker_registry.json')
copycfg=replace(cfg,data_root=out/'restart_copy');copystore=CsvStorage(copycfg)
files=[p for p in copycfg.data_root.rglob('*.json') if p.name in ('calendar.json','coverage.json')];before={str(p):(p.read_bytes(),p.stat().st_mtime_ns) for p in files}
original_read=copystore.read
def no_read(asset,tf='1h'):
 if copystore.path_for(asset,tf).exists():raise AssertionError('second startup reread prices '+asset+'/'+tf)
 return original_read(asset,tf)
copystore.read=no_read
class NoProvider:
 def get_history(self,*args,**kwargs):raise AssertionError('second startup requested provider')
start=time.monotonic();HistorySynchronizer(copycfg,NoProvider(),copystore,refresh_on_start=True)
assert all(before[str(p)]==(p.read_bytes(),p.stat().st_mtime_ns) for p in files),'second startup rewrote metadata'
summary['second_startup']={'seconds_including_backup':round(time.monotonic()-start,3),'csv_reads':0,'provider_requests':0,'metadata_rewrites':0};print('RESTART',summary['second_startup'],flush=True)
async def main():
 before=(root/'download_control.csv').read_bytes()
 async with streamablehttp_client('http://127.0.0.1:8010/mcp') as (r,w,_):
  async with ClientSession(r,w,read_timeout_seconds=timedelta(seconds=180)) as s:
   await s.initialize()
   async def call(asset,tf,live=False):
    args={'asset':asset,'timeframe':tf,'count':5}
    if not live:args['timestamp']='2026-08-30T00:00:00Z'
    start=time.monotonic();response=await s.call_tool('asset_bars',args);value=response.structuredContent or json.loads('\n'.join(x.text for x in response.content if x.type=='text'))
    assert not response.isError and value.get('bars'),value
    assert 'receipt_evidence' not in json.dumps(value)
    raw=store.read(asset,tf);cutoff=pd.Timestamp(value['requested_at'])
    for bar in value['bars']:
     stamp=pd.Timestamp(bar.get('source_timestamp',bar['t']));successor=bool(((raw.index>stamp)&(raw.index<=cutoff)).any())
     if successor:assert bar['is_bar_complete'] and bar['completion_reason']=='successor_received'
     elif bar['is_bar_complete']:
      boundary=pd.Timestamp(bar['completion_boundary']);ready=boundary+finalization_delay(cfg,asset);receipt=raw.attrs['receipts'][stamp.isoformat()]
      assert cutoff>=ready and pd.Timestamp(receipt['request_started_at'])>=ready
      assert receipt['ohlcv']==[float(raw.loc[stamp,k]) for k in ('open','high','low','close','volume')]
      assert bar['completion_reason'] in ('session_boundary_delay','period_boundary_delay')
    row={'asset':asset,'timeframe':tf,'live':live,'seconds':round(time.monotonic()-start,3),'last_reason':value['bars'][-1]['completion_reason']};summary['requests'].append(row)
    (out/f'{len(summary["requests"]):02d}.json').write_text(json.dumps(value,indent=2));print(json.dumps(row),flush=True)
   for asset,tfs in assets.items():
    for tf in tfs:await call(asset,tf)
   assert (root/'download_control.csv').read_bytes()==before,'cached requests downloaded'
   for tf in ('1h','4h','1D','1W'):await call('RUS:MX1!',tf,True)
 for asset in assets:
  folder=store.path_for(asset).parent;cov=read_json(folder/'coverage.json',{})
  assert cov['metadata_schema']==2 and 'result' not in cov.get('startup_check',{})
  assert not list(folder.glob('*.receipts.json'))
 summary['failures']=[];(out/'summary.json').write_text(json.dumps(summary,indent=2));print('PASS',out,flush=True)
asyncio.run(main())

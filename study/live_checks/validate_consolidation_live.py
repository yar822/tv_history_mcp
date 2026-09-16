import asyncio,json,time,hashlib
from pathlib import Path
from datetime import datetime,timezone,timedelta
from zipfile import ZipFile
import pandas as pd
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from tv_history.config import load_settings
from tv_history.storage import CsvStorage,read_json
from tv_history.finality import finalization_delay
cfg=load_settings(); store=CsvStorage(cfg); root=cfg.data_root
out=root/'live_checks'/datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_consolidation_live');out.mkdir(parents=True)
assets=json.loads((root/'ticker_registry.json').read_text()); failures=[]; records=[]; audit={}
def check(ok,msg):
 if not ok: failures.append(msg)
def audit_files():
 total=0
 for asset,tfs in assets.items():
  folder=store.path_for(asset).parent; cov=read_json(folder/'coverage.json',{}); evidence=cov.get('receipt_evidence',{})
  check((folder/'calendar.json').exists(),asset+' calendar missing')
  check(not list(folder.glob('*.receipts.json')),asset+' legacy receipts remain')
  check(not list(folder.glob('*.revisions.jsonl')),asset+' legacy revisions remain')
  for tf in tfs:
   check(bool(evidence.get(tf)),asset+'/'+tf+' missing evidence'); total+=len(evidence.get(tf,{}))
  audit[asset]={tf:len(evidence.get(tf,{})) for tf in tfs}
 return total
async def main():
 total=audit_files(); print('MIGRATION',total,audit,flush=True)
 # Compare pre-migration archive when one is available.
 for backup in sorted((root/'backups').glob('*.zip'),reverse=True):
  with ZipFile(backup) as z:
   names=z.namelist(); legacy=[n for n in names if n.endswith('.receipts.json')]
   if not legacy: continue
   preserved=0; unchanged=0
   for asset,tfs in assets.items():
    folder=store.path_for(asset).parent; cov=read_json(folder/'coverage.json',{})
    for tf in tfs:
     suffix=(folder.relative_to(root)/f'{tf}.receipts.json').as_posix()
     matches=[n for n in legacy if n.endswith(suffix)]
     if matches:
      old=json.loads(z.read(matches[0])); new=cov['receipt_evidence'][tf]
      for stamp,receipt in old.items():
       check(stamp in new,asset+'/'+tf+' lost receipt '+stamp)
       check(new.get(stamp)==receipt,asset+'/'+tf+' receipt changed before validation '+stamp)
      preserved+=len(old)
     suffix=store.path_for(asset,tf).relative_to(root).as_posix(); matches=[n for n in names if n.endswith(suffix)]
     if matches:
      same=z.read(matches[0])==store.path_for(asset,tf).read_bytes(); check(same,asset+'/'+tf+' CSV changed since backup');unchanged+=int(same)
   audit['backup_comparison']={'backup':str(backup),'receipts':preserved,'unchanged_csvs':unchanged}
   break
 before=(root/'download_control.csv').read_bytes()
 async with streamablehttp_client('http://127.0.0.1:8010/mcp') as (r,w,_):
  async with ClientSession(r,w,read_timeout_seconds=timedelta(seconds=180)) as session:
   await session.initialize()
   async def call(asset,tf,live=False):
    args={'asset':asset,'timeframe':tf,'count':5}
    if not live: args['timestamp']='2026-09-08T20:00:00Z'
    start=time.monotonic(); response=await session.call_tool('asset_bars',args)
    value=response.structuredContent or json.loads('\n'.join(c.text for c in response.content if c.type=='text'))
    row={'asset':asset,'timeframe':tf,'live':live,'seconds':round(time.monotonic()-start,3)}
    check(not response.isError and 'error' not in value,str(row)+' response error')
    check('receipt_evidence' not in json.dumps(value),str(row)+' receipt evidence exposed')
    bars=value.get('bars',[]); check(bool(bars),str(row)+' empty bars')
    raw=store.read(asset,tf); cutoff=pd.Timestamp(value['requested_at'])
    for bar in bars:
     stamp=pd.Timestamp(bar.get('source_timestamp',bar['t'])); successor=bool(((raw.index>stamp)&(raw.index<=cutoff)).any())
     complete=bar['is_bar_complete']; reason=bar['completion_reason']
     check(isinstance(complete,bool),str(row)+' flag type')
     if successor: check(complete and reason=='successor_received',str(row)+' successor completion mismatch')
     elif complete:
      boundary=pd.Timestamp(bar['completion_boundary']); receipt=raw.attrs['receipts'].get(stamp.isoformat(),{})
      check(reason in ('session_boundary_delay','period_boundary_delay'),str(row)+' unexpected completion')
      check(cutoff>=boundary+finalization_delay(cfg,asset),str(row)+' premature completion')
      check(pd.Timestamp(receipt.get('request_started_at'))>=boundary+finalization_delay(cfg,asset),str(row)+' stale evidence')
      check(receipt.get('ohlcv')==[float(raw.loc[stamp,k]) for k in ('open','high','low','close','volume')],str(row)+' fingerprint mismatch')
    row['completion_reasons']=[b['completion_reason'] for b in bars]; records.append(row)
    (out/f'{len(records):02d}.json').write_text(json.dumps(value,indent=2),encoding='utf-8'); print(json.dumps(row),flush=True)
   for asset,tfs in assets.items():
    for tf in tfs: await call(asset,tf)
   audit['cached_requests_provider_downloads']= (root/'download_control.csv').read_bytes()!=before
   check(not audit['cached_requests_provider_downloads'],'Cached requests triggered downloads')
   for tf in ('1h','4h','1D','1W'): await call('RUS:MX1!',tf,True)
 audit['evidence_after_live']=audit_files()
 result={'audit':audit,'requests':records,'failures':failures}
 (out/'summary.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
 (out/'report.md').write_text('# Live consolidation validation\n\n'+f'- Endpoint: http://127.0.0.1:8010/mcp\n- Assets: {len(assets)}; cached requests: 32; current-time requests: 4.\n- Receipt records at start: {total}.\n- Failures: {len(failures)}.\n- Legacy per-timeframe receipt and revision files absent before and after live requests.\n- Response completion flags checked against raw successors or boundary delay and receipt evidence.\n- Receipt maps absent from public responses.\n\nSee summary.json for timings and backup comparison.\n',encoding='utf-8')
 print('RESULT',str(out),json.dumps(failures),flush=True)
asyncio.run(main())

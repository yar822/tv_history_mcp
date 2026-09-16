import asyncio,json,re,time
from datetime import datetime,timezone,timedelta
from pathlib import Path
import pandas as pd
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from tv_history.config import load_settings
from tv_history.storage import CsvStorage
from tv_history.provider import TvDatafeedProvider
cfg=load_settings();store=CsvStorage(cfg);out=cfg.data_root/'live_checks'/datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_hourly_timestamp');out.mkdir(parents=True);summary={};assets=['COMEX:GC1!','RUS:MX1!','RUS:SI1!']
def rows(f):return [{'t':t.isoformat(),'c':float(r['close'])} for t,r in f.tail(7).iterrows()]
for asset in assets:summary[asset]={'before_cache':rows(store.read(asset,'1h'))}
async def main():
 async with streamablehttp_client('http://127.0.0.1:8010/mcp') as (r,w,_):
  async with ClientSession(r,w,read_timeout_seconds=timedelta(seconds=90)) as s:
   await s.initialize()
   for asset in assets:
    v=await s.call_tool('asset_bars',{'asset':asset,'timeframe':'1h','count':7})
    value=v.structuredContent or json.loads('\n'.join(c.text for c in v.content if c.type=='text'))
    summary[asset]['mcp']=value;print('MCP',asset,json.dumps(value.get('bars',value)),flush=True)
asyncio.run(main())
from tvDatafeed import TvDatafeed
original=TvDatafeed._TvDatafeed__create_df
wire={}
def capture(raw,symbol):
 values=[]
 for m in re.finditer(r'"v":\[([^\]]+)\]',raw):
  try:
   v=json.loads('['+m.group(1)+']')
   if len(v)>=5 and isinstance(v[0],(int,float)) and v[0]>1000000000:
    values.append({'epoch':v[0],'utc':datetime.fromtimestamp(v[0],timezone.utc).isoformat(),'close':v[4]})
  except (ValueError,TypeError):pass
 wire[symbol]=values
 f=original(raw,symbol)
 if f is not None:wire[symbol+'_naive']=[str(t) for t in f.index[-7:]]
 return f
TvDatafeed._TvDatafeed__create_df=staticmethod(capture)
p=TvDatafeedProvider(cfg)
for asset in assets:
 f=p.get_history(asset,'1h',7);summary[asset]['direct_provider']=rows(f);print('DIRECT',asset,json.dumps(rows(f)),flush=True)
summary['wire']=wire;summary['checked_at']=datetime.now(timezone.utc).isoformat()
(out/'summary.json').write_text(json.dumps(summary,indent=2));print('OUTPUT',out);print('WIRE',json.dumps(wire))

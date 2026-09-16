import json, shutil, time
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from tv_history.config import load_settings
from tv_history.storage import CsvStorage, atomic_json
from tv_history.sync import HistorySynchronizer
from tv_history.bars import AssetBarsService
import tv_history.bars as bars_module
import tv_history.timestamp_profiles as profiles
import tv_history.trading_dates as dates
cfg=load_settings()
out=cfg.data_root/'live_checks'/'20260909_201841_cached_latency'
root=out/'profile_cache'; root.mkdir(parents=True,exist_ok=True)
asset='CME_MINI:NQ1!'
original=CsvStorage(cfg).path_for(asset).parent
copy=root/original.relative_to(cfg.data_root); copy.mkdir(parents=True,exist_ok=True)
for p in original.iterdir():
    if p.is_file(): shutil.copy2(p,copy/p.name)
atomic_json(root/'ticker_registry.json',{asset:['1h','4h','1D','1W']})
settings=replace(cfg,data_root=root)
class NoNetwork:
    calls=[]
    def get_history(self,*args):
        self.calls.append(args); raise RuntimeError('provider forbidden in local cached profiling')
provider=NoNetwork(); storage=CsvStorage(settings)
sync=HistorySynchronizer(settings,provider,storage)
service=AssetBarsService(settings,sync)
results=[]
for tf,count in [('1h',100),('1D',100),('1W',100),('1W',5)]:
    events=[]
    def wrap(name,func):
        def call(*args,**kwargs):
            start=time.perf_counter()
            try: return func(*args,**kwargs)
            finally:
                event={'name':name,'seconds':time.perf_counter()-start}
                if args and isinstance(args[0],pd.DataFrame): event['input_rows']=len(args[0])
                if name=='read': event['timeframe']=args[1]
                events.append(event)
        return call
    with ExitStack() as stack:
        targets=[(sync,'_served_view'),(sync,'_normalize_view'),(sync,'_audit'),(sync,'_build_calendar'),(sync,'_save_coverage'),(storage,'read'),(profiles,'normalize_daily'),(dates,'normalize_daily'),(dates,'normalize_weekly'),(bars_module,'completion_details')]
        for obj,name in targets:
            key=name
            if obj is profiles: key='profile.normalize_daily'
            if obj is dates: key='dates.'+name
            stack.enter_context(patch.object(obj,name,wrap(key,getattr(obj,name))))
        stack.enter_context(patch('tv_history.sync.time.sleep',lambda _:None))
        started=time.perf_counter(); value=service.get_bars(asset,tf,'2026-09-04T12:00:00Z',count)
        seconds=time.perf_counter()-started
    aggregate=defaultdict(lambda:{'calls':0,'seconds':0,'input_rows':[]})
    for event in events:
        row=aggregate[event['name']]; row['calls']+=1; row['seconds']+=event['seconds']
        if 'input_rows' in event: row['input_rows'].append(event['input_rows'])
    result={'timeframe':tf,'count':count,'seconds':seconds,'error':value.get('error'),'bars_returned':value.get('bars_returned'),'timings':dict(aggregate),'events':events}
    results.append(result)
    (out/'local_profile.json').write_text(json.dumps({'provider_calls':provider.calls,'results':results},indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!='events'}),flush=True)
assert not provider.calls, provider.calls

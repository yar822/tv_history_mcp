import sys,json,shutil,time,hashlib
from pathlib import Path
from dataclasses import replace
from threading import RLock
root=Path(__file__).resolve().parents[2]
out=Path((root/'data/live_checks/active_speed_check.txt').read_text())
mode=sys.argv[1]
sys.path.insert(0,str(root/'study/live_checks'/out.name/'baseline_src' if mode=='before' else root/'src'))
from tv_history.config import load_settings
from tv_history.sync import HistorySynchronizer
from tv_history.storage import CsvStorage, DownloadControlLog
from tv_history.bars import AssetBarsService
from tv_history.analysis import AssetAnalysisService
from tv_history.chart import AssetChartService,ChartResult
cfg=replace(load_settings(root/'config.yaml'),data_root=out/(mode+'_cache'))
shutil.copytree(out/'snapshot',cfg.data_root)
class NoNetwork:
    calls=[]
    def get_history(self,*args):
        self.calls.append(args); raise RuntimeError('unexpected provider call in frozen cached comparison')
sync=HistorySynchronizer.__new__(HistorySynchronizer)
sync.settings=cfg; sync.provider=NoNetwork(); sync.storage=CsvStorage(cfg)
sync.control_log=DownloadControlLog(cfg.data_root); sync._lock=RLock()
sync._initialized=json.loads((cfg.data_root/'ticker_registry.json').read_text())
sync._last_fetch={}; sync.coverage={}; sync.calendars={}
for asset in sync._initialized:
    folder=sync.storage.path_for(asset).parent
    sync.coverage[asset]=json.loads((folder/'coverage.json').read_text())
    sync.calendars[asset]=json.loads((folder/'calendar.json').read_text())
bars=AssetBarsService(cfg,sync); analysis=AssetAnalysisService(cfg,sync); chart=AssetChartService(cfg,sync,sync.storage)
cases=[]
for asset in sync._initialized:
    for tf in ('1h','4h','1D','1W'):
        cases.append(('bars',asset,tf,5,None))
for tf in ('1h','1D','1W'): cases.append(('bars','CME_MINI:NQ1!',tf,100,None))
cases.extend([('bars','CME_MINI:NQ1!','1h',100,3),('analysis','CME_MINI:NQ1!','1D',None,None),('analysis','CME_MINI:NQ1!','1W',None,None),('chart','CME_MINI:NQ1!','1h',None,None)])
results=[]
for number,(tool,asset,tf,count,sessions) in enumerate(cases):
    started=time.perf_counter()
    stamp='2026-09-04T12:00:00Z'
    if tool=='bars': response=bars.get_bars(asset,tf,stamp,count,sessions)
    elif tool=='analysis': response=analysis.analyze(asset,tf,stamp)
    else:
        result=chart.render(asset,tf,stamp,5)
        response={'metadata':result.metadata,'image_sha256':hashlib.sha256(result.image_bytes).hexdigest()} if isinstance(result,ChartResult) else result
    row={'number':number,'tool':tool,'asset':asset,'tf':tf,'count':count,'sessions':sessions,'seconds':time.perf_counter()-started,'response':response}
    results.append(row)
    (out/(mode+'.json')).write_text(json.dumps({'source':str(sys.modules['tv_history.sync'].__file__),'provider_calls':sync.provider.calls,'results':results},indent=2))
    print(mode,number,tool,asset,tf,round(row['seconds'],3),'error',response.get('error'),flush=True)
assert not sync.provider.calls,sync.provider.calls
print('DONE',mode,len(results),out,flush=True)

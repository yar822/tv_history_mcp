import sys,json
from pathlib import Path
from dataclasses import replace
from threading import RLock
import tv_history.server as server
from tv_history.sync import HistorySynchronizer
from tv_history.storage import CsvStorage,DownloadControlLog
from tv_history.bars import AssetBarsService
from tv_history.analysis import AssetAnalysisService
from tv_history.chart import AssetChartService
root=Path(sys.argv[1]); port=int(sys.argv[2])
cfg=replace(server.settings,data_root=root)
class NoNetwork:
    def get_history(self,*args):
        with (root/'unexpected_provider_calls.jsonl').open('a') as f: f.write(json.dumps(args)+'\n')
        raise RuntimeError('provider forbidden in frozen HTTP test')
sync=HistorySynchronizer.__new__(HistorySynchronizer)
sync.settings=cfg; sync.provider=NoNetwork(); sync.storage=CsvStorage(cfg)
sync.control_log=DownloadControlLog(root); sync._lock=RLock()
sync._initialized=json.loads((root/'ticker_registry.json').read_text())
sync._last_fetch={}; sync.coverage={}; sync.calendars={}
for asset in sync._initialized:
    folder=sync.storage.path_for(asset).parent
    sync.coverage[asset]=json.loads((folder/'coverage.json').read_text())
    sync.calendars[asset]=json.loads((folder/'calendar.json').read_text())
server.synchronizer=sync; server.service=AssetAnalysisService(cfg,sync)
server.bars_service=AssetBarsService(cfg,sync); server.chart_service=AssetChartService(cfg,sync,sync.storage)
server.mcp.settings.host='127.0.0.1'; server.mcp.settings.port=port
server.mcp.run(transport='streamable-http')

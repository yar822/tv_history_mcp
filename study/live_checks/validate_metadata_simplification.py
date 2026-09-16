import json,shutil
from pathlib import Path
from dataclasses import replace
from datetime import datetime,timezone
import pandas as pd
from tv_history.config import load_settings
from tv_history.storage import CsvStorage,read_json,atomic_json
from tv_history.metadata import compact_calendar,expand_calendar
from tv_history.finality import completion_details,finalization_delay
from tv_history.calendar import DURATIONS
cfg=load_settings(); live=CsvStorage(cfg); out=cfg.data_root/'live_checks'/datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_metadata_simplification');out.mkdir(parents=True)
copy=CsvStorage(replace(cfg,data_root=out/'cache'));records=[];comparisons=0
for asset,tfs in json.loads((cfg.data_root/'ticker_registry.json').read_text()).items():
 src=live.path_for(asset).parent;dest=copy.path_for(asset).parent;shutil.copytree(src,dest)
 # Legacy folder names are resolved by CsvStorage after the copy.
 before_cov=(dest/'coverage.json').stat().st_size;before_cal=(dest/'calendar.json').stat().st_size
 original=expand_calendar(read_json(dest/'calendar.json',{})); compact=compact_calendar(original); expanded=expand_calendar(compact)
 for key,rule in original.get('rules',{}).items():
  assert expanded['rules'][key]['offset']==list(rule['offset'])
  assert expanded['rules'][key]['next_offset']==list(rule['next_offset'])
 prices={tf:copy.read(asset,tf) for tf in tfs};csvs={tf:copy.path_for(asset,tf).read_bytes() for tf in tfs}
 copy.migrate_metadata(asset);atomic_json(dest/'calendar.json',compact)
 for tf in tfs:
  old=prices[tf];new=copy.read(asset,tf)
  assert csvs[tf]==copy.path_for(asset,tf).read_bytes()
  effective=pd.Timestamp(original['effective_from'])
  cutoffs=[effective-pd.Timedelta(seconds=1),effective,effective+pd.Timedelta(days=7)]
  for stamp in old.index[-6:]:cutoffs.extend([stamp,stamp+DURATIONS[tf],stamp+DURATIONS[tf]+finalization_delay(cfg,asset)])
  for cutoff in cutoffs:
   selected=old.loc[old.index<=cutoff].tail(5).copy();selected.attrs={}
   selected.attrs['completion_context']={'calendar':original,'timeframe':tf,'raw_opens':list(old.index),'receipts':old.attrs['receipts']}
   a=completion_details(selected,DURATIONS[tf],cutoff,finalization_delay(cfg,asset))
   selected.attrs['completion_context'].update(calendar=expanded,receipts=new.attrs['receipts'])
   b=completion_details(selected,DURATIONS[tf],cutoff,finalization_delay(cfg,asset));assert a.equals(b),(asset,tf,cutoff);comparisons+=1
 records.append({'asset':asset,'coverage_before':before_cov,'coverage_after':(dest/'coverage.json').stat().st_size,'calendar_before':before_cal,'calendar_after':(dest/'calendar.json').stat().st_size,'receipts_after':sum(len(copy.read(asset,tf).attrs['receipts']) for tf in tfs)})
result={'records':records,'completion_comparisons':comparisons,'production_modified':False}
(out/'summary.json').write_text(json.dumps(result,indent=2));print(str(out));print(json.dumps(result,indent=2))

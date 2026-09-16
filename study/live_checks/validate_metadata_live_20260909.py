import asyncio
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import time
from zipfile import ZipFile

import pandas as pd
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from tv_history.config import load_settings
from tv_history.storage import CsvStorage, read_json, validate_frame
from tv_history.finality import finalization_delay
from tv_history.provider import split_asset

cfg = load_settings()
store = CsvStorage(cfg)
root = cfg.data_root
out = root/'live_checks'/datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_metadata_live_validation')
out.mkdir(parents=True)
assets = {
    'BITSTAMP:BTCUSD': ('UTC', 'utc_calendar'),
    'CME_MINI:NQ1!': ('America/Chicago', 'cme_overnight'),
    'COMEX:GC1!': ('America/Chicago', 'cme_overnight'),
    'COMEX:SI1!': ('America/Chicago', 'cme_overnight'),
    'ICEEUR:BRN1!': ('Europe/London', 'ice_brent'),
    'RUS:MX1!': ('Europe/Moscow', 'moex_futures'),
    'RUS:SBER': ('Europe/Moscow', None),
    'RUS:SI1!': ('Europe/Moscow', 'moex_futures'),
}
records, failures, versions = [], [], {}
snapshot = {}


def check(ok, message):
    if not ok:
        failures.append(message)


def save():
    (out/'summary.json').write_text(json.dumps({'snapshot':snapshot,'records':records,'failures':failures},indent=2),encoding='utf-8')


def audit_startup():
    backup = sorted((root/'backups').glob('*.zip'))[-1]
    backup_start = pd.Timestamp(datetime.strptime(backup.stem,'%Y%m%d_%H%M%S_%f'),tz='UTC')
    with ZipFile(backup) as archive:
        check(archive.testzip() is None, 'backup CRC failure')
        check('ticker_registry.json' in archive.namelist(), 'backup missing registry')
    log = pd.read_csv(root/'download_control.csv')
    selected = log[pd.to_datetime(log.input_timestamp,utc=True) >= backup_start]
    snapshot['backup'] = str(backup)
    snapshot['startup_log'] = selected.to_dict('records')
    snapshot['log_rows_before_calls'] = len(log)
    check(selected.empty, 'unexpected startup downloads for unchanged cached files')
    snapshot['assets'] = {}
    for asset, (zone, profile) in assets.items():
        parent = store.path_for(asset).parent
        calendar = read_json(parent/'calendar.json',{})
        coverage = read_json(parent/'coverage.json',{})
        metadata = coverage.get('startup_check',{})
        versions[asset] = calendar['version']
        check(bool(metadata), f'{asset}: missing startup metadata')
        check(calendar['timezone'] == zone, f'{asset}: startup timezone')
        check(pd.Timestamp(metadata['checked_at']) < backup_start, f'{asset}: metadata not reused')
        check(pd.Timestamp(calendar['built_at']) < backup_start, f'{asset}: calendar not reused')
        check(pd.Timestamp(metadata['through'])-pd.Timestamp(metadata['from']) == pd.Timedelta(days=100), f'{asset}: incorrect scan window')
        if calendar['status'] == 'ready':
            check(calendar['eligible_sessions'] == 30, f'{asset}: calendar session count')
        tf_snapshot = {}
        for tf in ('1h','4h','1D','1W'):
            stat=store.path_for(asset,tf).stat()
            check(metadata['files'][tf]=={'size':stat.st_size,'mtime_ns':stat.st_mtime_ns},f'{asset}/{tf}: startup file signature')
            raw = store.read(asset,tf)
            validate_frame(raw)
            check(not raw.empty, f'{asset}/{tf}: empty cache')
            state=coverage.get(tf,{})
            check(not state.get('refresh_error'),f'{asset}/{tf}: persisted refresh failure')
            tf_snapshot[tf] = {'rows':len(raw),'first':str(raw.index.min()),'last':str(raw.index.max()),
                               'confirmed_gaps':sum(g['status']=='incomplete' for g in state.get('unresolved_gaps',[])),
                               'uncertain_gaps':sum(g['status']=='uncertain' for g in state.get('unresolved_gaps',[]))}
        snapshot['assets'][asset] = {'calendar':calendar,'startup_check':metadata,'timeframes':tf_snapshot}
    recovered = pd.read_csv(root/'investigations/nq_gap_20260909/recoverable_gap_bars.csv',parse_dates=['timestamp_utc'])
    check(set(recovered.timestamp_utc).issubset(set(store.read('CME_MINI:NQ1!','1h').index)), 'NQ recovered timestamps missing after restart')
    snapshot['nq_recovered_bars'] = len(recovered)


async def main():
    print('OUTPUT',str(out),flush=True)
    for _ in range(180):
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1',8010)
            writer.close()
            await writer.wait_closed()
            break
        except OSError:
            await asyncio.sleep(1)
    else:
        raise RuntimeError('MCP did not finish startup within three minutes')
    async with streamablehttp_client('http://127.0.0.1:8010/mcp') as (read, write, _):
        async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=180)) as session:
            await session.initialize()
            audit_startup()
            save()
            async def call(tool,args,expected_error=None):
                begin=time.monotonic()
                tag=f'{len(records):02d}_{tool}_{args["asset"].replace(":","-").replace("!","")}_{args.get("timeframe","1h")}'
                try:
                    response=await session.call_tool(tool,args)
                    value=response.structuredContent or json.loads('\n'.join(c.text for c in response.content if c.type=='text'))
                    (out/(tag+'.json')).write_text(json.dumps(value,indent=2),encoding='utf-8')
                    error=value.get('error',{}).get('code')
                    check(error==expected_error,f'{tag}: unexpected error {error}')
                    if expected_error is None and not error:
                        check('history_coverage' in value,f'{tag}: missing coverage')
                        symbol, exchange = split_asset(value['asset'])
                        asset=f'{exchange}:{symbol}'
                        cal=value.get('completion_calendar',{})
                        check(cal.get('version')==versions[asset],f'{tag}: runtime calendar changed')
                        check(cal.get('timezone')==assets[asset][0],f'{tag}: wrong timezone')
                        tf=args.get('timeframe','1h')
                        if tool=='asset_bars':
                            bars=value['bars']
                            if args.get('timestamp')!='2000-01-01T00:00:00Z':
                                check(bool(bars),f'{tag}: no bars')
                            if tf in ('1D','1W'):
                                check(value.get('timestamp_normalization',{}).get('profile')==assets[asset][1],f'{tag}: wrong profile')
                            raw=store.read(asset,tf)
                            cutoff=pd.Timestamp(value['requested_at'])
                            for bar in bars:
                                stamp=pd.Timestamp(bar.get('source_timestamp',bar['t']))
                                reason=bar['completion_reason']
                                check(isinstance(bar['is_bar_complete'],bool),f'{tag}: completion flag type')
                                check(stamp<=cutoff,f'{tag}: future source bar')
                                successor=any((raw.index>stamp)&(raw.index<=cutoff))
                                if reason=='successor_received':
                                    check(bar['is_bar_complete'] and successor,f'{tag}: missing successor')
                                elif bar['is_bar_complete']:
                                    check(reason in ('session_boundary_delay','period_boundary_delay'),f'{tag}: unjustified completed bar')
                                    boundary=pd.Timestamp(bar['completion_boundary'])
                                    check(cutoff>=boundary+finalization_delay(cfg,asset),f'{tag}: completed before delay')
                                    receipt=raw.attrs['receipts'].get(stamp.isoformat(),{})
                                    check(pd.Timestamp(receipt['request_started_at'])>=boundary+finalization_delay(cfg,asset),f'{tag}: stale completion receipt')
                                else:
                                    check(not successor,f'{tag}: ignored successor')
                        if tool=='asset_chart':
                            check(value['is_bar_complete'] is True,f'{tag}: incomplete chart')
                            check(any(c.type=='image' for c in response.content),f'{tag}: no chart image')
                    row={'tool':tool,'args':args,'seconds':round(time.monotonic()-begin,2),'error':error,
                         'coverage':value.get('history_coverage',{}).get('status')}
                    if tool=='asset_bars' and value.get('bars'):
                        row['latest']={k:value['bars'][-1].get(k) for k in ('t','source_timestamp','is_bar_complete','completion_reason','completion_boundary')}
                    records.append(row)
                    print(json.dumps(row),flush=True)
                    save()
                    return value
                except Exception as exc:
                    failures.append(f'{tag}: {exc!r}')
                    records.append({'tool':tool,'args':args,'exception':repr(exc)})
                    save()
                    return {}
            for asset in assets:
                for tf in ('1h','4h','1D','1W'):
                    await call('asset_bars',{'asset':asset,'timeframe':tf,'count':5})
                await call('asset_analysis',{'asset':asset,'timeframe':'1h'})
                await call('asset_chart',{'asset':asset,'timeframe':'1h','sessions':1})
            for asset in ('CME_MINI:NQ1!','BITSTAMP:BTCUSD'):
                await call('asset_analysis',{'asset':asset,'timeframe':'1D'})
            alias=await call('asset_bars',{'asset':'sber:rus','timeframe':'1D','count':5})
            check(split_asset(alias.get('asset',''))==('SBER','RUS'),'SBER alias resolution')
            check('SBER:RUS' not in json.loads((root/'ticker_registry.json').read_text()),'duplicate alias registry entry')
            nq=await call('asset_bars',{'asset':'CME_MINI:NQ1!','timeframe':'1h','timestamp':'2026-08-24T12:00:00Z','count':6})
            check(nq.get('bars',[{}])[-1].get('t')=='2026-08-24T12:00:00Z','NQ returns stale pre-gap price')
            check(nq.get('history_coverage',{}).get('status')=='no_known_gaps','NQ repaired window coverage')
            await call('asset_analysis',{'asset':'CME_MINI:NQ1!','timeframe':'1h','timestamp':'2026-08-24T12:00:00Z'})
            args={'asset':'CME_MINI:NQ1!','timeframe':'1h','timestamp':'2000-01-01T00:00:00Z','count':3}
            missing=await call('asset_bars',args)
            check(missing.get('history_coverage',{}).get('status')=='incomplete','pre-cache coverage')
            size=(root/'download_control.csv').stat().st_size
            await call('asset_bars',args)
            check((root/'download_control.csv').stat().st_size==size,'repeated impossible backfill download')
            await call('asset_analysis',{k:v for k,v in args.items() if k!='count'},'INSUFFICIENT_HISTORY_COVERAGE')
            save()
    log=pd.read_csv(root/'download_control.csv')
    snapshot['runtime_downloads']=log.iloc[snapshot['log_rows_before_calls']:].to_dict('records')
    snapshot['metadata_after_requests']={}
    for asset in assets:
        parent=store.path_for(asset).parent
        cov=read_json(parent/'coverage.json',{})
        cal=read_json(parent/'calendar.json',{})
        meta=cov['startup_check']
        changed=[]
        for tf in ('1h','4h','1D','1W'):
            stat=store.path_for(asset,tf).stat()
            if meta['files'][tf]!={'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}: changed.append(tf)
        check(cal['version']==versions[asset],f'{asset}: final calendar changed')
        check(meta['checked_at']==snapshot['assets'][asset]['startup_check']['checked_at'],f'{asset}: ordinary calls incorrectly certified startup metadata')
        snapshot['metadata_after_requests'][asset]={'changed_timeframes':changed,'calendar_reused':cal['version']==versions[asset]}
    save()
    print('RESULT',json.dumps({'calls':len(records),'failures':failures,'directory':str(out)}),flush=True)


asyncio.run(main())

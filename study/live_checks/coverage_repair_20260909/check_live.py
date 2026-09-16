import asyncio
from datetime import timedelta
import json
from pathlib import Path
import time

import pandas as pd
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from tv_history.config import load_settings
from tv_history.storage import CsvStorage

OUT = Path(__file__).resolve().parents[3] / 'data/live_checks/coverage_repair_20260909'
ROOT = load_settings().data_root
records = []


async def main():
    for _ in range(120):
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', 8010)
            writer.close()
            await writer.wait_closed()
            break
        except OSError:
            await asyncio.sleep(1)
    else:
        raise RuntimeError('MCP did not finish startup')
    async with streamablehttp_client('http://127.0.0.1:8010/mcp') as (read, write, _):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=180)) as session:
            await session.initialize()

            async def call(tool, args, expected_error=None):
                started = time.monotonic()
                response = await session.call_tool(tool, args)
                value = response.structuredContent or json.loads('\n'.join(c.text for c in response.content if c.type == 'text'))
                (OUT / f'{len(records):02d}_{tool}.json').write_text(json.dumps(value, indent=2), encoding='utf-8')
                if expected_error:
                    assert value['error']['code'] == expected_error, value
                else:
                    assert 'error' not in value, value
                    assert 'history_coverage' in value
                row = {'tool': tool, 'args': args, 'seconds': round(time.monotonic()-started, 2),
                       'coverage': value.get('history_coverage', {}).get('status'),
                       'error': value.get('error', {}).get('code')}
                records.append(row)
                print(json.dumps(row), flush=True)
                return value

            for asset in json.loads((ROOT/'ticker_registry.json').read_text()):
                await call('asset_bars', {'asset': asset, 'timeframe': '1h', 'count': 10})
            nq = await call('asset_bars', {'asset':'CME_MINI:NQ1!', 'timeframe':'1h', 'timestamp':'2026-08-24T12:00:00Z', 'count':6})
            assert pd.Timestamp(nq['bars'][-1]['t']) == pd.Timestamp('2026-08-24T12:00Z')
            assert nq['history_coverage']['status'] == 'no_known_gaps'
            await call('asset_analysis', {'asset':'CME_MINI:NQ1!', 'timeframe':'1h', 'timestamp':'2026-08-24T12:00:00Z'})
            await call('asset_chart', {'asset':'CME_MINI:NQ1!', 'timeframe':'1h', 'timestamp':'2026-08-24T12:00:00Z', 'days':1})
            old_args = {'asset':'CME_MINI:NQ1!', 'timeframe':'1h', 'timestamp':'2000-01-01T00:00:00Z', 'count':3}
            old = await call('asset_bars', old_args)
            assert old['history_coverage']['status'] == 'incomplete' and not old['bars']
            size = (ROOT/'download_control.csv').stat().st_size
            await call('asset_bars', old_args)
            assert (ROOT/'download_control.csv').stat().st_size == size
            await call('asset_analysis', {k:v for k,v in old_args.items() if k != 'count'}, 'INSUFFICIENT_HISTORY_COVERAGE')

    store = CsvStorage(load_settings())
    recovered = pd.read_csv(ROOT/'investigations/nq_gap_20260909/recoverable_gap_bars.csv', parse_dates=['timestamp_utc'])
    cached = store.read('CME_MINI:NQ1!', '1h')
    assert set(recovered.timestamp_utc).issubset(set(cached.index))
    summary = {'records': records, 'nq_recovered_timestamps_verified': len(recovered), 'assets': {}}
    for asset in json.loads((ROOT/'ticker_registry.json').read_text()):
        c = json.loads((store.path_for(asset).parent/'coverage.json').read_text())
        summary['assets'][asset] = {tf: {'rows':len(store.read(asset,tf)),
            'confirmed_gaps':sum(g['status']=='incomplete' for g in state['unresolved_gaps']),
            'uncertain_gaps':sum(g['status']=='uncertain' for g in state['unresolved_gaps']),
            'refresh_error':state.get('refresh_error')} for tf,state in c.items()}
    (OUT/'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print('VERIFIED',len(records),'MCP calls;',len(recovered),'NQ bars restored', flush=True)


asyncio.run(main())

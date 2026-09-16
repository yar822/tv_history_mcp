import asyncio
import base64
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = (Path(__file__).resolve().parents[2] / 'data/live_checks') / datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
ROOT.mkdir(parents=True, exist_ok=True)
records = []
failures = []
versions = {}


def check(condition, message):
    if not condition:
        failures.append(message)


async def call(session, name, args):
    begin = datetime.now(timezone.utc)
    try:
        result = await asyncio.wait_for(session.call_tool(name, args), timeout=240)
        value = result.structuredContent or json.loads('\n'.join(c.text for c in result.content if c.type == 'text'))
        label = f'{len(records):02d}_{name}_{args["asset"].replace(":", "-").replace("!", "")}_{args.get("timeframe", "1h")}'
        (ROOT / (label + '.json')).write_text(json.dumps(value, indent=2), encoding='utf-8')
        for content in result.content:
            if content.type == 'image':
                binary = base64.b64decode(content.data)
                check(binary.startswith(b'\x89PNG\r\n\x1a\n'), f'{label}: invalid PNG')
                (ROOT / (label + '.png')).write_bytes(binary)
        row = {'tool': name, 'arguments': args, 'started_at': begin.isoformat(),
               'seconds': round((datetime.now(timezone.utc)-begin).total_seconds(), 2), 'saved': label+'.json'}
        if result.isError or 'error' in value:
            row['error'] = value.get('error', 'MCP error')
            failures.append(f'{label}: {row["error"]}')
        else:
            cal = value.get('completion_calendar', {})
            check(bool(cal.get('version')), f'{label}: missing calendar metadata')
            asset = args['asset']
            if asset in versions:
                check(versions[asset] == cal.get('version'), f'{asset}: calendar rebuilt during refresh')
            versions[asset] = cal.get('version')
            row['calendar'] = cal
            if name == 'asset_bars':
                bars = value.get('bars', [])
                check(bool(bars), f'{label}: no bars')
                evaluated = datetime.fromisoformat(value['requested_at'])
                times = [datetime.fromisoformat(b.get('source_timestamp', b['t']).replace('Z', '+00:00')) for b in bars]
                for i, bar in enumerate(bars):
                    reason = bar.get('completion_reason')
                    flag = bar.get('is_bar_complete')
                    check(isinstance(flag, bool) and isinstance(reason, str), f'{label}: missing completion contract')
                    check(times[i] <= evaluated, f'{label}: future source opening')
                    if flag:
                        check(reason in {'successor_received', 'session_boundary_delay', 'period_boundary_delay'}, f'{label}: invalid true reason')
                        if reason == 'successor_received':
                            check(any(t > times[i] and t <= evaluated for t in times), f'{label}: no visible successor for {bar["t"]}')
                    else:
                        check(reason not in {'successor_received', 'session_boundary_delay', 'period_boundary_delay'}, f'{label}: contradictory false reason')
                row['tail'] = [{k:b.get(k) for k in ('t','source_timestamp','c','is_bar_complete','completion_reason','completion_boundary')} for b in bars[-2:]]
            else:
                row['status'] = {k:value.get(k) for k in ('bar_open','bar_close','is_bar_complete','completion_reason','completion_boundary')}
                check(isinstance(value.get('completion_reason'), str), f'{label}: missing reason')
                if name == 'asset_chart':
                    check(value.get('is_bar_complete') is True, f'{label}: chart displays incomplete candle')
                    check(any(c.type == 'image' for c in result.content), f'{label}: missing chart image')
        records.append(row)
        print(json.dumps(row), flush=True)
    except Exception as exc:
        row = {'tool':name, 'arguments':args, 'error': repr(exc)}
        records.append(row)
        failures.append(str(row))
        print(json.dumps(row), flush=True)
    (ROOT/'summary.json').write_text(json.dumps({'records':records,'failures':failures},indent=2), encoding='utf-8')


async def main():
    print('ARTIFACT_DIRECTORY', str(ROOT), flush=True)
    async with streamablehttp_client('http://127.0.0.1:8010/mcp') as (read, write, _):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=240)) as session:
            await session.initialize()
            for tf in ('1h','4h','1D','1W'):
                await call(session, 'asset_bars', {'asset':'RUS:MX1!', 'timeframe':tf, 'count':5})
            for asset in ('RUS:SI1!', 'COMEX:GC1!', 'CME_MINI:NQ1!', 'ICEEUR:BRN1!', 'BITSTAMP:BTCUSD'):
                await call(session, 'asset_bars', {'asset':asset,'timeframe':'1h','count':5})
            await call(session,'asset_analysis',{'asset':'RUS:MX1!', 'timeframe':'1h'})
            await call(session,'asset_chart',{'asset':'RUS:MX1!', 'timeframe':'1h','sessions':2})
            await call(session,'asset_bars',{'asset':'RUS:MX1!', 'timeframe':'1h','count':5})
    print('RESULT', json.dumps({'requests':len(records),'failures':failures,'directory':str(ROOT)}), flush=True)


asyncio.run(main())

"""Validate shared historical session/completion boundaries, optionally live."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd

from tv_history.config import load_settings
from tv_history.finality import completion_details, finalization_delay
from tv_history.metadata import expand_calendar
from tv_history.storage import CsvStorage, read_json
from tv_history.trading_session import TradingSessionService

ASSETS = ['ICEEUR:BRN1!', 'COMEX:GC1!', 'CME_MINI:NQ1!', 'RUS:MX1!', 'RUS:SI1!', 'BITSTAMP:BTCUSD']
DATES = ['2025-10-27', '2025-11-03']


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    cfg = load_settings()
    storage = CsvStorage(cfg)
    service = TradingSessionService(storage)
    report = {'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'validation': 'live MCP and existing-cache evaluation' if args.live else 'existing-cache evaluation only',
              'local': [], 'live': [], 'checks': []}

    def check(name, passed):
        report['checks'].append({'name': name, 'passed': bool(passed)})

    paths = [storage.path_for(asset).parent / '1h.csv' for asset in ASSETS]
    hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    for asset in ASSETS:
        source = storage.read(asset, '1h')
        calendar = expand_calendar(read_json(storage.path_for(asset).parent / 'calendar.json', {}))
        source.attrs['completion_context'] = {'timeframe': '1h', 'calendar': calendar,
            'raw_opens': list(source.index), 'receipts': source.attrs.get('receipts', {}),
            'hourly_coverage': storage.read_coverage(asset).get('1h', {})}
        for date in DATES:
            session = service.get_sessions(asset, date=date)
            row = {'asset': asset, 'date': date, 'session': session}
            report['local'].append(row)
            if session['status'] != 'observed':
                continue
            start = pd.Timestamp(session['sessions'][0]['open']['datetime'])
            close = pd.Timestamp(session['sessions'][0]['close']['datetime'])
            selected = source.loc[(source.index >= start) & (source.index < close)]
            details = completion_details(selected, pd.Timedelta(hours=1), close, finalization_delay(cfg, asset))
            row.update(count=len(selected), complete=int(details.is_bar_complete.sum()), tail=details.iloc[-1].to_dict())
            check(f'{asset} {date} local all complete', details.is_bar_complete.all())
            before = completion_details(selected.tail(1), pd.Timedelta(hours=1), close - pd.Timedelta(seconds=1), finalization_delay(cfg, asset))
            check(f'{asset} {date} not early', not before.iloc[-1].is_bar_complete)

    if args.live:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        async with streamablehttp_client('http://127.0.0.1:8010/mcp', timeout=60, sse_read_timeout=60) as (read, write, _):
            async with ClientSession(read, write) as client:
                await client.initialize()
                schema = next(t.inputSchema for t in (await client.list_tools()).tools if t.name == 'trading_session')
                report['trading_session_schema'] = schema
                check('live date supported', 'date' in schema.get('properties', {}))

                async def call(name, arguments):
                    response = await client.call_tool(name, arguments)
                    payload = response.structuredContent or json.loads(next(c.text for c in response.content if c.type == 'text'))
                    check(f'{name} {arguments} success', not response.isError and 'error' not in payload)
                    return payload

                for local in report['local']:
                    asset, date = local['asset'], local['date']
                    session = await call('trading_session', {'asset': asset, 'date': date})
                    row = {'asset': asset, 'date': date, 'session': session}
                    report['live'].append(row)
                    check(f'{asset} {date} local/live session', session.get('sessions') == local['session'].get('sessions'))
                    if session.get('status') != 'observed':
                        continue
                    start = pd.Timestamp(session['sessions'][0]['open']['datetime'])
                    close = pd.Timestamp(session['sessions'][0]['close']['datetime'])
                    result = await call('asset_bars', {'asset': asset, 'timeframe': '1h', 'timestamp': close.isoformat(), 'count': 200})
                    bars = [b for b in result.get('bars', []) if start <= pd.Timestamp(b['t']) < close]
                    row.update(bars=bars, count=len(bars), complete=sum(b['is_bar_complete'] for b in bars))
                    check(f'{asset} {date} live all complete', len(bars) == local['count'] and all(b['is_bar_complete'] for b in bars))
                    if asset == 'ICEEUR:BRN1!':
                        before = await call('asset_bars', {'asset': asset, 'timeframe': '1h', 'timestamp': (close - pd.Timedelta(seconds=1)).isoformat(), 'count': 1})
                        row['before_close'] = before
                        check(f'{asset} {date} live not early', before['bars'][-1]['is_bar_complete'] is False)
                ongoing = await call('trading_session', {'asset': 'ICEEUR:BRN1!', 'date': datetime.now(timezone.utc).date().isoformat()})
                report['ongoing'] = ongoing
                check('ongoing remains expected', ongoing.get('status') == 'expected')
    check('hourly CSVs unchanged', all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest for p, digest in hashes.items()))
    report['passed'] = all(c['passed'] for c in report['checks'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'report': str(args.output), 'passed': report['passed'], 'checks': len(report['checks']),
                      'failures': [c for c in report['checks'] if not c['passed']],
                      'local_summary': [{k: r.get(k) for k in ('asset', 'date', 'count', 'complete', 'tail')} for r in report['local']]}, indent=2))
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())

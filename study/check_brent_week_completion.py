"""Inspect Brent's 2025-10-27 weekly tail and optional live MCP flags."""
import argparse
import asyncio
import json
from pathlib import Path

import pandas as pd

from tv_history.config import load_settings
from tv_history.finality import completion_details, finalization_delay
from tv_history.metadata import expand_calendar
from tv_history.storage import CsvStorage, read_json
from tv_history.trading_session import TradingSessionService
from tv_history.trading_weeks import trading_week_bounds


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    settings = load_settings()
    storage = CsvStorage(settings)
    asset = 'ICEEUR:BRN1!'
    source = storage.read(asset, '1h')
    calendar = expand_calendar(read_json(storage.path_for(asset).parent / 'calendar.json', {}))
    week = source.loc['2025-10-27':'2025-10-31']
    target = week.tail(1).copy()
    target.attrs['completion_context'] = {
        'timeframe': '1h', 'calendar': calendar, 'raw_opens': list(source.index),
        'receipts': source.attrs.get('receipts', {})}
    cutoffs = ['2025-10-31T22:00:00Z', '2025-10-31T23:00:00Z', '2025-11-03T01:00:00Z']
    report = {'asset': asset, 'week': '2025-10-27',
              'validation': 'Local existing-cache evaluation; live responses only when requested',
              'weekly_rule': calendar.get('trading_weeks'),
              'expected': trading_week_bounds(calendar, '2025-10-27'),
              'last_bar_open': target.index[-1].isoformat(),
              'last_bar_nominal_end': (target.index[-1] + pd.Timedelta(hours=1)).isoformat(),
              'next_cached_open': source.index[source.index > target.index[-1]][0].isoformat(),
              'dated_session': TradingSessionService(storage).get_sessions(asset, 'week', '2025-10-27'),
              'local_flags': [], 'live': []}
    for cutoff in cutoffs:
        detail = completion_details(target, pd.Timedelta(hours=1), pd.Timestamp(cutoff),
                                    finalization_delay(settings, asset)).iloc[0].to_dict()
        report['local_flags'].append({'cutoff': cutoff, **detail})
    if args.live:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        try:
            async with streamablehttp_client('http://127.0.0.1:8010/mcp', timeout=30, sse_read_timeout=30) as (read, write, _):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    for cutoff in cutoffs:
                        response = await client.call_tool('asset_bars', {
                            'asset': asset, 'timeframe': '1h', 'timestamp': cutoff, 'count': 150})
                        payload = response.structuredContent or json.loads(next(c.text for c in response.content if c.type == 'text'))
                        week_bars = [bar for bar in payload.get('bars', [])
                                     if pd.Timestamp('2025-10-26T22:00:00Z') <= pd.Timestamp(bar['t'])
                                     < pd.Timestamp('2025-11-01T00:00:00Z')]
                        summary = {'total': len(week_bars),
                                   'complete': sum(bar['is_bar_complete'] is True for bar in week_bars),
                                   'incomplete': [bar for bar in week_bars if bar['is_bar_complete'] is not True]}
                        report['live'].append({'cutoff': cutoff, 'isError': response.isError,
                                               'week_summary': summary, 'response': payload})
        except Exception as exc:
            report['live_error'] = str(exc)
    report['checks'] = {
        'observed_close_differs_from_rule': report['last_bar_nominal_end'] != report['expected']['end_utc'],
        'local_tail_flags': [row['is_bar_complete'] for row in report['local_flags']] == [False, False, True],
    }
    if report['live']:
        target_stamp = target.index[-1]
        tails = [next((bar for bar in row['response'].get('bars', [])
                       if pd.Timestamp(bar['t']) == target_stamp), {}) for row in report['live']]
        report['checks']['live_calls_succeeded'] = len(report['live']) == 3 and all(
            not row['isError'] and 'error' not in row['response'] for row in report['live'])
        report['checks']['live_tail_flags'] = [bar.get('is_bar_complete') for bar in tails] == [False, False, True]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'report': str(args.output), 'checks': report['checks'],
                      'week_summaries': [{'cutoff': row['cutoff'], **row['week_summary']} for row in report['live']],
                      'live_error': report.get('live_error')}, indent=2))


if __name__ == '__main__':
    asyncio.run(main())

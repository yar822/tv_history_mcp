"""Read-only production investigation; replay writes only in a temporary directory."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import pandas as pd
from tv_history.config import load_settings
from tv_history.provider import TvDatafeedProvider
from tv_history.storage import CsvStorage, atomic_json
from tv_history.sync import HistorySynchronizer
from tv_history.execution import data_quality

out = Path(__file__).resolve().parents[3] / 'data/investigations/nq_gap_20260909'
cfg = load_settings()
asset = 'CME_MINI:NQ1!'
left = pd.Timestamp('2026-08-19T06:00Z')
right = pd.Timestamp('2026-08-28T17:00Z')
full = CsvStorage(cfg).read(asset, '1h')
full.attrs = {}
old = full.loc[:left].copy()
returned = full.loc[right:'2026-09-06T06:54Z'].copy()

class Replay:
    def __init__(self):
        self.calls = []
    def get_history(self, asset, timeframe, n_bars):
        self.calls.append((asset, timeframe, n_bars))
        return returned.tail(n_bars).copy()

with TemporaryDirectory(prefix='nq_gap_review_') as temp:
    isolated = replace(cfg, data_root=Path(temp), market_rules={})
    store = CsvStorage(isolated)
    for tf in ('1h', '4h', '1D', '1W'):
        store.merge_and_write(asset, old if tf == '1h' else old.tail(1), tf)
    atomic_json(isolated.data_root / 'ticker_registry.json', {asset: ['1h', '4h', '1D', '1W']})
    provider = Replay()
    sync = HistorySynchronizer(isolated, provider, store)
    _, first = sync.ensure_available(asset, '1h', pd.Timestamp('2026-08-24T00:00Z'))
    persisted = store.read(asset, '1h')
    _, second = sync.ensure_available(asset, '1h', pd.Timestamp('2026-08-24T00:00Z'))
    assert provider.calls == [(asset, '1h', 119)]
    assert len(persisted.loc['2026-08-24']) == 0
    assert second['reason'] == 'requested_timestamp_is_covered'
    replay = {'calls': provider.calls, 'first': first, 'second': second,
              'target_day_rows': 0, 'gap_reproduced': True}
    print('REPLAY', replay, flush=True)

summary = {'replay': replay,
           'boundary_quality': data_quality(full.loc[[left, right]], '1h', asset)}
# Separate provider instance and output file: does not touch production CSVs/logs.
started = pd.Timestamp.now(tz='UTC')
fresh = TvDatafeedProvider(cfg).get_history(asset, '1h', 5000)
fresh.to_csv(out / 'fresh_1h.csv', index_label='timestamp_utc')
missing = fresh.loc[(fresh.index > left) & (fresh.index < right)]
missing.to_csv(out / 'recoverable_gap_bars.csv', index_label='timestamp_utc')
summary['fresh_download'] = {
    'started_at': started.isoformat(), 'received_at': pd.Timestamp.now(tz='UTC').isoformat(),
    'requested': 5000, 'received': len(fresh),
    'first': str(fresh.index.min()), 'last': str(fresh.index.max()),
    'bars_inside_gap': len(missing), 'missing_from_cache': len(missing.index.difference(full.index)),
    'first_inside_gap': str(missing.index.min()), 'last_inside_gap': str(missing.index.max()),
}
(out / 'evidence.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
print(json.dumps(summary, indent=2), flush=True)

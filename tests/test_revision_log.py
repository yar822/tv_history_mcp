import json
import pandas as pd
from tv_history.storage import CsvStorage
from test_sync import make_settings, frame

ASSET = 'RUS:MX1!'

def test_last_bar_updates_are_not_revisions(tmp_path):
    store = CsvStorage(make_settings(tmp_path))
    for tf in ('1h','4h','1D','1W'):
        store.merge_and_write(ASSET, frame('2026-09-01', [100.]), tf)
        store.merge_and_write(ASSET, frame('2026-09-01', [101.,102.]), tf, pd.Timestamp('2026-09-02T00:00Z'))
    assert not (store.path_for(ASSET).parent/'revisions.jsonl').exists()
    assert store.read(ASSET).iloc[0]['close'] == 101.

def test_older_revisions_share_one_log_with_timeframe(tmp_path):
    store = CsvStorage(make_settings(tmp_path))
    for tf in ('1h','4h','1D','1W'):
        store.merge_and_write(ASSET, frame('2026-09-01', [100.,102.]), tf)
        store.merge_and_write(ASSET, frame('2026-09-01', [101.,103.]), tf, pd.Timestamp('2026-09-02T00:00Z'))
    folder=store.path_for(ASSET).parent
    rows=[json.loads(line) for line in (folder/'revisions.jsonl').read_text().splitlines()]
    assert [row['timeframe'] for row in rows] == ['1h','4h','1D','1W']
    assert all(row['before']['close']==100. and row['after']['close']==101. for row in rows)
    assert not list(folder.glob('*.revisions.jsonl'))

def test_migration_preserves_records_and_is_idempotent(tmp_path):
    store=CsvStorage(make_settings(tmp_path))
    store.merge_and_write(ASSET,frame('2026-09-01',[100.]))
    folder=store.path_for(ASSET).parent
    record={'asset':ASSET,'timeframe':'1h','observed_at':'2026-09-01T00:00Z','before':{'close':99.},'after':{'close':100.}}
    text=json.dumps(record)+'\n'
    (folder/'revisions.jsonl').write_text(text)
    (folder/'1h.revisions.jsonl').write_text(text)
    record['timeframe']='4h'
    (folder/'4h.revisions.jsonl').write_text(json.dumps(record)+'\n')
    for _ in range(2): store.merge_and_write(ASSET,frame('2026-09-01',[100.]))
    assert len((folder/'revisions.jsonl').read_text().splitlines())==2
    assert not list(folder.glob('*.revisions.jsonl'))

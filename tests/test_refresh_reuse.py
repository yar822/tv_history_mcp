from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from test_analysis import settings
import pandas as pd
from test_history_coverage import seeded, Provider, ASSET
from test_sync import frame
from tv_history.bars import AssetBarsService


def test_concurrent_monitors_share_fetch_and_keep_own_windows(tmp_path):
    prices = frame('2026-09-10T09:00Z', [100.,101.,102.])
    provider = Provider([prices])
    cfg, storage, sync = seeded(tmp_path, prices, provider)
    cutoff = pd.Timestamp('2026-09-10T11:20Z')
    service = AssetBarsService(replace(cfg,indicators=settings(tmp_path).indicators),sync)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda n: service.get_bars(ASSET,'1h',cutoff.isoformat(),count=n), [1,2,3]))
    assert len(provider.calls) == 1
    assert all('bars' in r for r in results), results
    assert [len(r['bars']) for r in results] == [1,2,3]
    assert all(r['bars'][-1] == results[0]['bars'][-1] for r in results)
    receipts = storage.read(ASSET,'1h').attrs['receipts']
    service.get_bars(ASSET,'1h',(cutoff-pd.Timedelta(hours=1)).isoformat(),count=1)
    assert storage.read(ASSET,'1h').attrs['receipts'] == receipts
    assert len(provider.calls) == 1


def test_reuse_expires_at_ten_seconds_and_is_per_timeframe(tmp_path,monkeypatch):
    tick=[100.]
    monkeypatch.setattr('tv_history.sync.time.monotonic',lambda:tick[0])
    prices=frame('2026-09-10T09:00Z',[100.,101.,102.])
    provider=Provider([prices,prices.tail(1),prices])
    _,_,sync=seeded(tmp_path,prices,provider)
    cutoff=pd.Timestamp('2026-09-10T11:20Z')
    sync.ensure_available(ASSET,'1h',cutoff,count=1)
    tick[0]=109.999
    sync.ensure_available(ASSET,'1h',cutoff+pd.Timedelta(seconds=1),count=1)
    assert len(provider.calls)==1
    sync.ensure_available(ASSET,'4h',cutoff,count=1)
    assert len(provider.calls)==2
    tick[0]=110.
    sync.ensure_available(ASSET,'1h',cutoff,count=1)
    assert len(provider.calls)==3


def test_failure_does_not_enable_reuse(tmp_path,monkeypatch):
    prices=frame('2026-09-10T09:00Z',[100.,101.,102.])
    provider=Provider([prices])
    _,_,sync=seeded(tmp_path,prices,provider)
    original=sync._download_with_retries
    def failed(*args):raise RuntimeError('unavailable')
    monkeypatch.setattr(sync,'_download_with_retries',failed)
    cutoff=pd.Timestamp('2026-09-10T11:20Z')
    sync.ensure_available(ASSET,'1h',cutoff,count=1)
    assert (ASSET,'1h') not in sync._successful_refresh_at
    monkeypatch.setattr(sync,'_download_with_retries',original)
    sync.ensure_available(ASSET,'1h',cutoff,count=1)
    assert len(provider.calls)==1

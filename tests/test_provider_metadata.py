import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from tv_history import provider_metadata as adapter
from tv_history.sync import HistorySynchronizer
from tv_history.storage import CsvStorage
from test_sync import make_settings


class Socket:
    def __init__(self, response):
        self.response = response
        self.sent = []
        self.closed = False

    def send(self, payload):
        self.sent.append(json.loads(payload.split('~m~')[-1]))

    def settimeout(self, timeout):
        pass

    def recv(self):
        session = self.sent[-1]['p'][0]
        msg = self.response(session)
        return '~m~1~m~' + json.dumps(msg)

    def close(self):
        self.closed = True


@pytest.mark.parametrize('token,delay,expected', [('test-token',600,True), ('unauthorized_user_token',900,False), ('test-token',None,True)])
def test_symbol_lookup_only(monkeypatch, token, delay, expected):
    ws = Socket(lambda session: {'m':'symbol_resolved','p':[session,'symbol_1',{'full_name':'COMEX_DL:GC1!', **({'delay':delay} if delay is not None else {})}]})
    monkeypatch.setattr(adapter,'create_connection',lambda *a,**kw:ws)
    result = adapter.collect_metadata('COMEX:GC1!',token)
    assert result == {'resolved_symbol':'COMEX_DL:GC1!','delay_seconds':delay,'auth_token':expected}
    assert [m['m'] for m in ws.sent] == ['set_auth_token','chart_create_session','resolve_symbol']
    assert ws.closed


def test_rejected_token_and_network_failure(monkeypatch):
    ws = Socket(lambda session: {'m':'protocol_error','p':['bad auth token']})
    monkeypatch.setattr(adapter,'create_connection',lambda *a,**kw:ws)
    assert adapter.collect_metadata('RUS:SI1!','invalid')['auth_token'] is False
    def failed(*a,**kw): raise TimeoutError('must not be exposed')
    monkeypatch.setattr(adapter,'create_connection',failed)
    assert adapter.collect_metadata('RUS:SI1!','test') == adapter.unknown_metadata()


def test_per_ticker_concurrency_persistence_and_restart(tmp_path):
    calls = []
    def lookup(asset):
        calls.append(asset)
        return {'resolved_symbol':asset,'delay_seconds':600 if asset.startswith('COMEX') else None,'auth_token':True}
    cfg = make_settings(tmp_path)
    storage = CsvStorage(cfg)
    provider = SimpleNamespace(get_metadata=lookup)
    sync = HistorySynchronizer(cfg,provider,storage)
    assets = ['COMEX:GC1!', 'RUS:SI1!'] * 5
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(sync.get_provider_metadata,assets))
    assert sorted(calls) == ['COMEX:GC1!', 'RUS:SI1!']
    for asset,result in zip(assets,results):
        assert result['resolved_symbol'] == asset
        assert storage.read_coverage(asset)['provider_metadata'] == result
    results[0]['delay_seconds'] = -1
    assert sync.get_provider_metadata('GC1!:COMEX')['delay_seconds'] == 600
    restarted = HistorySynchronizer(cfg,provider,storage)
    assert restarted.get_provider_metadata('COMEX:GC1!')['delay_seconds'] == 600
    assert calls.count('COMEX:GC1!') == 2


def test_response_metadata(monkeypatch):
    from tv_history import server
    expected = {'resolved_symbol':'RUS:SI1!','delay_seconds':None,'auth_token':True}
    monkeypatch.setattr(server,'synchronizer',SimpleNamespace(get_provider_metadata=lambda asset:expected.copy()))
    for result in ({'bars':[]},{'price_data':{}},{'coverage_from':'test'}):
        asyncio.run(server.add_provider_metadata(result,'RUS:SI1!'))
        assert result['provider_metadata'] == expected


def test_all_mcp_tools_expose_metadata(monkeypatch):
    from tv_history import server
    from tv_history.chart import ChartResult
    expected = {'resolved_symbol':'COMEX_DL:GC1!','delay_seconds':600,'auth_token':True}
    monkeypatch.setattr(server,'synchronizer',SimpleNamespace(get_provider_metadata=lambda asset:expected.copy()))
    monkeypatch.setattr(server,'service',SimpleNamespace(analyze=lambda *a:{'price_data':{}}))
    monkeypatch.setattr(server,'bars_service',SimpleNamespace(get_bars=lambda *a:{'bars':[]}))
    monkeypatch.setattr(server,'chart_service',SimpleNamespace(render=lambda *a:ChartResult(metadata={},image_bytes=b'png')))
    for call in (server.asset_analysis,server.asset_bars,server.asset_chart):
        result = asyncio.run(call('COMEX:GC1!'))
        assert result.structuredContent['provider_metadata'] == expected
        assert json.loads(result.content[0].text)['provider_metadata'] == expected


def test_startup_collects_known_ticker_once_even_without_bars(tmp_path):
    from tv_history.storage import atomic_json
    cfg = make_settings(tmp_path)
    atomic_json(cfg.data_root / 'ticker_registry.json', {'COMEX:GC1!':[]})
    calls = []
    def lookup(asset):
        calls.append(asset)
        return adapter.unknown_metadata()
    sync = HistorySynchronizer(cfg,SimpleNamespace(get_metadata=lookup),CsvStorage(cfg),refresh_on_start=True)
    sync.get_provider_metadata('COMEX:GC1!')
    assert calls.count('COMEX:GC1!') == 1

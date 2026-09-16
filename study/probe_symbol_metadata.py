"""Inspect message shapes only; never print authentication payloads."""
import sys
import json
import time
from pathlib import Path
from websocket import create_connection

token = (Path(__file__).resolve().parents[1] / '_token.txt').read_text().strip()
if '--invalid' in sys.argv: token = 'invalid-test-token'
ws = create_connection('wss://data.tradingview.com/socket.io/websocket', origin='https://data.tradingview.com', timeout=5)
try:
    for method, params in [('set_auth_token',[token]),('chart_create_session',['cs_metadataprobe','']),('resolve_symbol',['cs_metadataprobe','symbol_1','COMEX:GC1!'])]:
        payload=json.dumps({'m':method,'p':params},separators=(',',':'))
        ws.send(f'~m~{len(payload)}~m~{payload}')
    end=time.monotonic()+5
    while time.monotonic()<end:
        try: raw=ws.recv()
        except Exception: break
        for part in raw.split('~m~'):
            try: msg=json.loads(part)
            except ValueError: continue
            if not isinstance(msg,dict): continue
            print('message:',msg.get('m'),'parameter shapes:',[list(p) if isinstance(p,dict) else type(p).__name__ for p in msg.get('p',[])])
            if msg.get('m') in ('critical_error','protocol_error'): print('error fields:', [p for p in msg.get('p',[]) if isinstance(p,str) and p != token])
            if msg.get('m')=='symbol_resolved':
                print('metadata:',{k:v for k,v in msg['p'][2].items() if k in ('full_name','delay')})

finally:
    ws.close()

"""Live metadata-only check; does not update production history or print tokens."""
import json
import sys
from pathlib import Path
from tv_history.provider_metadata import collect_metadata

token = (Path(__file__).resolve().parents[1] / '_token.txt').read_text(encoding='utf-8-sig').strip()
for asset in (sys.argv[1:] or ('RUS:SI1!', 'COMEX:GC1!')):
    print(json.dumps({'asset':asset, 'provider_metadata':collect_metadata(asset,token)}))

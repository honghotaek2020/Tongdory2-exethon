"""제자리 목록화(S2-A) 회귀 평가: 데모 AI 협업 저장소를 실제 Liner API로 분석해 manifest와 비교.

실행: TONGDORY_HOME=demo/home TONGDORY_DATA_DIR=demo/.appdata uv run python scripts/eval_inplace.py
"""
import asyncio, json
from pathlib import Path
from tongdory2.config import get_settings
from tongdory2.db import DB
from tongdory2.fsx.paths import real
from tongdory2.fsx.roots import Role
from tongdory2.fsx.scope import Scope
from tongdory2.liner.client import LinerClient
from tongdory2.spec.store import SpecStore
from tongdory2.catalog.inplace import scan_inplace, map_virtual
async def main():
    s=get_settings(); db=DB(s.db_path); roots=db.list_roots(); spec=SpecStore(db.conn).current()
    home=real(s.home); ip=next(r for r in roots if r.role==Role.IN_PLACE)
    ents, summ = scan_inplace(ip, Scope(roots, home, s.data_dir), home)
    print(summ)
    async with LinerClient(s.liner_api_key, s.liner_base_url, conn=db.conn, run_id='eval-ip') as c:
        await map_virtual(ents, spec, c); cost=c.total_cost_usd
    man={i['home_rel']:i for i in json.load(open('demo/_manifest.json'))['items'] if i.get('in_place')}
    ok=0
    for e in ents:
        rel=str(Path(e.path).relative_to(home)); m=man.get(rel,{})
        good = e.verdict==m.get('expected_verdict') and (m.get('expected_virtual_node_id') in (None, e.node_id)) and (m.get('expected_doc_type') in (None, e.doc_type)) and m.get('expected_git_status')==e.git_status
        ok+=good
        print('OK ' if good else 'XX ', m.get('no'), rel.split('qrpay-settle-batch/')[-1], e.verdict, m.get('expected_verdict'), e.doc_type, m.get('expected_doc_type'), e.node_id, m.get('expected_virtual_node_id'), e.git_status, m.get('expected_git_status'), f'{e.confidence:.2f}')
    print(f'{ok}/{len(man)} cost ${cost:.4f}')
asyncio.run(main())

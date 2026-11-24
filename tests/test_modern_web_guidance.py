"""전체 검사와 실행 범위, 호출 비용 원장의 범위 혼합을 방지한다."""
from dataclasses import asdict
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient
from tongdory2.catalog.pipeline import Entry
from tongdory2.catalog.store import CatalogStore
from tongdory2.config import Settings
from tongdory2.liner.client import API_CALLS_DDL
from tongdory2.plan.executor import save_plan
from tongdory2.spec.store import SpecStore
from tongdory2.web.app import create_app
from tongdory2.web.routes.home import _liner_totals
from tongdory2.web.setup_state import scope_key


def test_usage_aggregates_same_ledger_excluding_balance_and_cached_costs():
    conn = sqlite3.connect(':memory:')
    assert _liner_totals(conn) == dict(external=0, cached=0, unreported=0, cost=0)
    conn.executescript(API_CALLS_DDL)
    conn.executemany('INSERT INTO api_calls(call_id,run_id,endpoint,cost_kind,cost_usd) VALUES(?,?,?,?,?)', [
        ('a','old','/decisions','REPORTED',0.1),
        ('b','old','/rerank','ESTIMATED',0.2),
        ('c','new','/decisions','CACHED',99),
        ('d','new','/chat/completions','NONE',None),
        ('e','new','/credits/balance','REPORTED',100),
        ('f',None,'/tools/search/web','FLAT',0.3),
    ])
    total = _liner_totals(conn)
    assert (total['external'],total['cached'],total['unreported']) == (4,1,1)
    assert total['cost'] == pytest.approx(0.6)
    assert _liner_totals(conn,'new') == dict(external=1,cached=1,unreported=1,cost=0)
    assert _liner_totals(conn,'missing')['external'] == 0


def test_home_execution_summary_keeps_first_batch_when_later_batches_exist(tmp_path):
    home = tmp_path/'home'
    (home/'Desktop').mkdir(parents=True)
    (home/'Documents'/'저장').mkdir(parents=True)
    client = TestClient(create_app(Settings(_env_file=None,TONGDORY_HOME=str(home),
        TONGDORY_DATA_DIR=str(tmp_path/'data'),LINER_API_KEY='')),base_url='http://127.0.0.1:8765')
    client.post('/setup/structure', data={'paths':'보고/회의록'})
    client.post('/setup/sources', data={'paths':'~/Desktop'})
    client.post('/setup/destination',data={'path':'~/Documents/저장'})
    t=client.app.state.t
    spec=SpecStore(t.db.conn).current()
    store=CatalogStore(t.db.conn)
    runs=[]
    for number,count in ((1,2),(2,7)):
        rid=store.start(spec['version'],dict(scope_key=scope_key(t.db.list_roots(),spec),
            series_id='test-series',batch_number=number))
        entries=[asdict(Entry(str(i),str(home/'Desktop'/f'{number}-{i}.txt'),f'{number}-{i}.txt',
            f'{number}-{i}.txt','source','source',verdict='HOLD',reason='판단 근거')) for i in range(count)]
        store.finish(rid,entries,0)
        runs.append(rid)
    plan=save_plan(t.db.conn,runs[0],dict(total=2,runs=[runs[0]],ops=[],skipped=[],
        review_snapshot=[dict(entry_id=str(i),bucket='check',selected=False) for i in range(2)]))
    t.db.conn.execute("INSERT INTO batches VALUES('batch-first',?,'DONE','{}','2026-11-22T10:00:00',NULL)",(plan,))
    t.db.conn.executescript(API_CALLS_DDL)
    t.db.conn.execute("INSERT INTO api_calls(call_id,run_id,endpoint,cost_kind,cost_usd) VALUES('old-call',?,'/decisions','REPORTED',0.25)",(runs[0],))
    t.db.conn.commit()
    page=client.get('/').text
    assert '정리 완료' in page and '전체 2개 · ' in page  # 숫자는 블록·버튼에만, 안내 줄은 실행 대상만
    assert '파일 9개 분류 →' not in page
    assert '분류 묶음 1 처리 완료' in page
    assert '2번째 묶음 처리 완료' not in page
    assert '최근 분류 묶음 2 · 내 PC 분류 7개' in page
    assert '저장된 전체 작업 · 외부 호출 1회' in page
    assert '저장된 전체 작업 합계 · 외부 호출 1회' in page
    assert '이번 묶음 9개' not in page
    review=client.get('/s2?result=1&batch=1&tab=check').text
    assert '판단 근거' in review and '분류 근거 보기' in review
    assert '<nav class="tabs" aria-label="분류 결과">' in review
    assert 'role="tab"' not in review
    assert len(re.findall('<h1\\b',review)) == 1
    assert '<dialog class="notice soft-past-bar"' in review

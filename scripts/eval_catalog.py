"""카탈로그 회귀 평가: 가상 데모 로컬 결과와 선택 분석 결과를 기대값과 비교.

전제: demo/.appdata DB에 S0 대상 확정 + S1 명세 확정이 되어 있어야 한다 (웹 화면에서 1회 진행).
실행: TONGDORY_HOME=demo/home TONGDORY_DATA_DIR=demo/.appdata uv run python scripts/eval_catalog.py
가상 데모의 확인 필요 문서를 실제 Liner에 보내 평가하려면 --with-liner를 추가한다.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

from tongdory2.catalog.pipeline import analyze, document_candidate, entries_to_dicts, load_document, prepare
from tongdory2.config import get_settings
from tongdory2.db import DB
from tongdory2.fsx.paths import real
from tongdory2.fsx.roots import Role
from tongdory2.fsx.scope import Scope
from tongdory2.liner.client import LinerClient
from tongdory2.spec.store import SpecStore

ROOT = Path(__file__).resolve().parent.parent


async def main() -> int:
    s = get_settings()
    db = DB(s.db_path)
    roots = db.list_roots()
    spec = SpecStore(db.conn).current()
    if not roots or spec is None:
        print("대상 확정·명세 확정을 먼저 하세요")
        return 2
    home = real(s.home)
    scope = Scope(roots, home, s.data_dir)
    prep = prepare(roots, scope, spec, home, s.recent_hours)
    print("로컬 검사:", prep.notice)
    entries, cost = prep.entries, 0.0
    if "--with-liner" in sys.argv:
        if home != real(ROOT / "demo/home"):
            print("가상 데모 홈을 지정한 경우에만 평가용 Liner 분석을 실행할 수 있습니다.")
            return 2
        for entry, item, body in zip(entries, prep.items, entries_to_dicts(entries)):
            if document_candidate(body):
                load_document(entry, item, scope, s.recent_hours)
        if any(entry.verdict == "PENDING" for entry in entries):
            async with LinerClient(s.liner_api_key, s.liner_base_url, conn=db.conn, run_id="eval") as c:
                entries = await analyze(prep, c, [r.path.name for r in roots if r.role == Role.BASELINE])
                cost = c.total_cost_usd

    man = json.loads((ROOT / "demo/_manifest.json").read_text(encoding="utf-8"))
    exp = {it["home_rel"]: it for it in man["items"] if not it.get("in_place")}
    got = {str(Path(e.path).relative_to(home)): e for e in entries}
    ok = verdict_ok = 0
    rows = []
    for rel, it in sorted(exp.items(), key=lambda x: x[1]["no"] if isinstance(x[1]["no"], int) else 999):
        e = got.get(rel)
        if e is None:
            rows.append((it["no"], rel, it["expected_verdict"], "—없음—", it.get("expected_node_path"), "", ""))
            continue
        v_ok = e.verdict == it["expected_verdict"]
        n_ok = (it.get("expected_node_path") or None) in (None, e.node_path) or e.verdict in ("HOLD", "EXCLUDE", "DUP")
        verdict_ok += v_ok
        ok += v_ok and n_ok
        rows.append((it["no"], rel[:46], it["expected_verdict"], e.verdict + ("" if v_ok else " ✗"),
                     (it.get("expected_node_path") or "")[:34], (e.node_path or e.reason_code or "")[:34] + ("" if n_ok else " ✗"),
                     f"{e.confidence:.2f}" if e.confidence > 0 else ""))
    for r in rows:
        print(" | ".join(str(x) for x in r))
    print(f"\n판정 일치 {verdict_ok}/{len(exp)} · 판정+목적지 일치 {ok}/{len(exp)} · 비용 ${cost:.4f}")
    print("분포:", Counter(e.verdict for e in entries))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

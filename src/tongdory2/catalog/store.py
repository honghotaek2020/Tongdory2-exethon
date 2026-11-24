"""카탈로그 실행 결과 저장 + 사람 지정(override)."""

from __future__ import annotations

import json
import sqlite3

from tongdory2.catalog.inventory import complete_selection
from tongdory2.catalog.pipeline import Prepared, auto_move as pipeline_auto_move, document_candidate, document_folder_text, document_folders
from tongdory2.db import new_id, now_iso
from tongdory2.fsx.scan import ScanItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_runs(
  run_id TEXT PRIMARY KEY, spec_version INTEGER, status TEXT NOT NULL,
  notice TEXT, cost_usd REAL DEFAULT 0, error TEXT, created_at TEXT NOT NULL, finished_at TEXT, kind TEXT NOT NULL DEFAULT 'COLLECT');
CREATE TABLE IF NOT EXISTS catalog_entries(
  run_id TEXT NOT NULL REFERENCES catalog_runs(run_id), entry_id TEXT NOT NULL, body TEXT NOT NULL,
  PRIMARY KEY(run_id, entry_id));
CREATE TABLE IF NOT EXISTS catalog_overrides(
  run_id TEXT NOT NULL, entry_id TEXT NOT NULL, target TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, entry_id));
CREATE TABLE IF NOT EXISTS catalog_review_revisions(
  run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS document_suggestions(
  project TEXT NOT NULL, path TEXT NOT NULL, fingerprint TEXT NOT NULL, node_key TEXT NOT NULL, body TEXT NOT NULL,
  PRIMARY KEY(project, path));
CREATE TABLE IF NOT EXISTS document_analysis_events(
  run_id TEXT NOT NULL REFERENCES catalog_runs(run_id), selected INTEGER NOT NULL, analyzed INTEGER NOT NULL,
  plan_before INTEGER NOT NULL, plan_after INTEGER NOT NULL, cost_usd REAL NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS document_analysis_run ON document_analysis_events(run_id);
"""

def _file_key(item: ScanItem) -> str:
    return json.dumps((item.root_id, item.device, item.inode, item.ctime_ns, item.mtime_ns, item.size))


def _node_key(node: dict) -> str:
    return json.dumps((node["node_id"], node["display_path"], node["profile"], document_folder_text(node)),
                      ensure_ascii=False)


class CatalogStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(catalog_runs)")}
        if "kind" not in cols:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                if "kind" not in {r[1] for r in conn.execute("PRAGMA table_info(catalog_runs)")}:
                    conn.execute("ALTER TABLE catalog_runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'COLLECT'")

    def start(self, spec_version: int, notice: dict, kind: str = "COLLECT") -> str:
        rid = new_id("run")
        with self.conn:
            self.conn.execute(
                "INSERT INTO catalog_runs(run_id, spec_version, status, notice, created_at, kind) VALUES(?,?,?,?,?,?)",
                (rid, spec_version, "RUNNING", json.dumps(notice, ensure_ascii=False), now_iso(), kind),
            )
        return rid

    def finish(self, run_id: str, entries: list[dict], cost: float, error: str | None = None,
               *, notice: dict | None = None, inventory_items: list[ScanItem] | None = None) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO catalog_entries VALUES(?,?,?)",
                [(run_id, e["entry_id"], json.dumps(e, ensure_ascii=False)) for e in entries],
            )
            self.conn.execute(
                "UPDATE catalog_runs SET status=?, cost_usd=?, error=?, finished_at=?, notice=COALESCE(?, notice) WHERE run_id=?",
                ("FAILED" if error else "DONE", cost, error, now_iso(),
                 json.dumps(notice, ensure_ascii=False) if notice is not None else None, run_id),
            )
            if not error and inventory_items is not None and notice is not None:
                complete_selection(self.conn, notice["series_id"], inventory_items)

    def update_notice(self, run_id: str, notice: dict) -> None:
        with self.conn:
            self.conn.execute("UPDATE catalog_runs SET notice=? WHERE run_id=?",
                              (json.dumps(notice, ensure_ascii=False), run_id))

    def apply_document_results(self, run_id: str, entries: list[dict], cost: float, notice: dict,
                               *, analyzed: Prepared | None = None, inventory: dict[str, ScanItem] | None = None,
                               plan_before: int, plan_after: int) -> None:
        suggestions = []
        if analyzed is not None and inventory is not None:
            nodes = {n["node_id"]: n for n in document_folders(analyzed.spec)}
            for entry in analyzed.entries:
                node = nodes.get(entry.node_id)
                item = inventory.get(entry.entry_id)
                if not node or not item or entry.verdict not in ("JOIN", "SPEC_NODE") or entry.node_path != node["display_path"]:
                    continue
                idea = {key: getattr(entry, key) for key in ("node_id", "reason", "confidence", "contested")}
                suggestions.append((str(analyzed.current_root.path), entry.path, _file_key(item), _node_key(node),
                                    json.dumps(idea, ensure_ascii=False)))
        with self.conn:
            self.conn.executemany("UPDATE catalog_entries SET body=? WHERE run_id=? AND entry_id=?",
                                  [(json.dumps(e, ensure_ascii=False), run_id, e["entry_id"]) for e in entries])
            self.conn.execute("UPDATE catalog_runs SET cost_usd=cost_usd+?, notice=? WHERE run_id=?",
                              (cost, json.dumps(notice, ensure_ascii=False), run_id))
            if suggestions:
                self.conn.executemany("INSERT OR REPLACE INTO document_suggestions VALUES(?,?,?,?,?)", suggestions)
            if analyzed is not None and analyzed.entries:
                self.conn.execute("INSERT INTO document_analysis_events VALUES(?,?,?,?,?,?,?)",
                                  (run_id, len(entries), len(analyzed.entries), plan_before, plan_after, cost, now_iso()))

    def document_analysis_history(self, limit: int = 20) -> list[dict]:
        """새 요청은 건별로, 이전 버전의 기록은 묶음 합계로 표시한다."""
        rows = self.conn.execute("""
            WITH recorded AS (
                SELECT run_id, SUM(analyzed) AS analyzed, SUM(cost_usd) AS cost
                FROM document_analysis_events GROUP BY run_id
            )
            SELECT run_id, selected, analyzed, plan_before, plan_after, cost_usd, created_at, rowid AS seq
            FROM document_analysis_events
            UNION ALL
            SELECT r.run_id, NULL, json_extract(r.notice, '$.send_names') - COALESCE(e.analyzed, 0),
                   NULL, NULL, MAX(0, r.cost_usd - COALESCE(e.cost, 0)),
                   COALESCE(r.finished_at, r.created_at), r.rowid
            FROM catalog_runs r LEFT JOIN recorded e ON r.run_id=e.run_id
            WHERE r.kind='COLLECT' AND r.status='DONE' AND json_extract(r.notice, '$.send_names') > COALESCE(e.analyzed, 0)
            ORDER BY created_at DESC, seq DESC LIMIT ?
        """, (limit,))
        columns = ("run_id", "selected", "analyzed", "plan_before", "plan_after", "cost", "time")
        return [dict(zip(columns, row)) for row in rows]

    def reuse_document_suggestions(self, prep: Prepared) -> int:
        """동일한 파일과 현재 명세에 남아 있는 폴더에만 이전 목적지 제안을 붙인다."""
        count = 0
        project = prep.current_root.path
        if not self.conn.execute("SELECT 1 FROM document_suggestions WHERE project=? LIMIT 1", (str(project),)).fetchone():
            return 0
        nodes = {n["node_id"]: n for n in document_folders(prep.spec)}
        for entry, item in zip(prep.entries, prep.items):
            if not document_candidate(vars(entry)):
                continue
            row = self.conn.execute(
                "SELECT fingerprint,node_key,body FROM document_suggestions WHERE project=? AND path=?",
                (str(project), entry.path),
            ).fetchone()
            if not row or row[0] != _file_key(item):
                continue
            idea = json.loads(row[2])
            node = nodes.get(idea["node_id"])
            if not node or row[1] != _node_key(node):
                continue
            entry.node_id, entry.node_path = node["node_id"], node["display_path"]
            entry.verdict = "JOIN" if (project / entry.node_path).is_dir() else "SPEC_NODE"
            entry.reason = idea["reason"]
            entry.confidence, entry.contested = idea["confidence"], idea["contested"]
            entry.reason_code = None
            entry.default_checked = pipeline_auto_move(entry) and not entry.contested  # 처음 분석 때와 같은 기준
            entry.candidates.clear()
            entry.badges.append("Liner 제안 재사용")
            count += 1
        return count

    def _run(self, condition: str, value: str) -> dict | None:
        r = self.conn.execute(
            "SELECT run_id, spec_version, status, notice, cost_usd, error, created_at, finished_at"
            f" FROM catalog_runs WHERE {condition}=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (value,),
        ).fetchone()
        if not r:
            return None
        keys = ("run_id", "spec_version", "status", "notice", "cost_usd", "error", "created_at", "finished_at")
        return dict(zip(keys, r))

    def series_runs(self, series_id: str) -> list[dict]:
        """한 분석(시리즈)의 묶음별 최신 실행 — 묶음 번호순. 같은 묶음을 다시 검사했으면 최근 것만."""
        keys = ("run_id", "spec_version", "status", "notice", "cost_usd", "error", "created_at", "finished_at")
        rows = self.conn.execute(
            "SELECT run_id, spec_version, status, notice, cost_usd, error, created_at, finished_at FROM catalog_runs "
            "WHERE kind='COLLECT' AND json_extract(notice, '$.series_id')=? ORDER BY created_at, rowid", (series_id,))
        newest: dict[int, dict] = {}
        for r in rows:
            run = dict(zip(keys, r))
            newest[json.loads(run["notice"] or "{}").get("batch_number", 0)] = run
        return [newest[k] for k in sorted(newest)]

    def latest(self, kind: str = "COLLECT") -> dict | None:
        return self._run("kind", kind)

    def get(self, run_id: str) -> dict | None:
        return self._run("run_id", run_id)

    def series_progress(self, series_id: str) -> set[tuple[str, int, int]]:
        """완료한 묶음에서 처리한 파일 경로와 변경 지문을 읽는다."""
        rows = self.conn.execute(
            "SELECT e.body FROM catalog_entries e JOIN catalog_runs r ON e.run_id=r.run_id "
            "WHERE r.kind='COLLECT' AND r.status='DONE' AND json_extract(r.notice, '$.series_id')=?",
            (series_id,),
        )
        processed = set()
        for (body,) in rows:
            entry = json.loads(body)
            processed.add((entry["path"], entry["size"], entry["mtime_ns"]))
        return processed

    def entries(self, run_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT body FROM catalog_entries WHERE run_id=? ORDER BY entry_id", (run_id,))
        out = [json.loads(b) for (b,) in rows]
        ov = dict(self.conn.execute("SELECT entry_id, target FROM catalog_overrides WHERE run_id=?", (run_id,)).fetchall())
        for e in out:
            e["override"] = ov.get(e["entry_id"])
        return out

    def entry(self, run_id: str, entry_id: str) -> dict | None:
        r = self.conn.execute("SELECT body FROM catalog_entries WHERE run_id=? AND entry_id=?", (run_id, entry_id)).fetchone()
        return json.loads(r[0]) if r else None

    def sensitive_paths(self) -> set[str]:
        """앞서 본문 또는 이름에서 L3으로 분류한 파일의 경로를 찾는다."""
        return {row[0] for row in self.conn.execute(
            """SELECT DISTINCT json_extract(e.body, '$.path')
               FROM catalog_entries e JOIN catalog_runs r ON r.run_id=e.run_id
               WHERE r.status='DONE' AND json_extract(e.body, '$.sensitivity')='L3'"""
        ) if row[0]}

    # ---- 재배치 체크 상태 (화면을 나눠 보여 줘도 서버에 남는다) ----
    MOVABLE = ("JOIN", "SPEC_NODE", "NEW_NODE", "GROUP", "ETC", "DUP")

    def _sel_table(self) -> None:
        self.conn.execute("""CREATE TABLE IF NOT EXISTS catalog_selection(
            run_id TEXT NOT NULL, entry_id TEXT NOT NULL, checked INTEGER NOT NULL,
            PRIMARY KEY(run_id, entry_id))""")

    def set_checked(self, run_id: str, entry_ids: list[str], checked: bool) -> None:
        if not entry_ids:
            return
        self._sel_table()
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO catalog_selection VALUES(?,?,?)",
                                  [(run_id, e, int(checked)) for e in entry_ids])
            self._advance_review(run_id)

    def _advance_review(self, run_id: str) -> None:
        self.conn.execute("INSERT INTO catalog_review_revisions VALUES(?,1)"
                          " ON CONFLICT(run_id) DO UPDATE SET revision=revision+1", (run_id,))

    def review_revision(self, run_id: str) -> int:
        row = self.conn.execute("SELECT revision FROM catalog_review_revisions WHERE run_id=?", (run_id,)).fetchone()
        return row[0] if row else 0

    def checked_map(self, run_id: str) -> dict[str, bool]:
        self._sel_table()
        return {e: bool(c) for e, c in self.conn.execute(
            "SELECT entry_id, checked FROM catalog_selection WHERE run_id=?", (run_id,))}

    def refresh_defaults(self, run_id: str) -> int:
        """예전 기준으로 저장된 결과도 지금 기준(추천 폴더가 있으면 기본 선택)으로 맞춘다.
        사용자가 직접 바꾼 체크(catalog_selection)는 그대로 두고, 기본값만 고친다."""
        cur = self.conn.execute(
            """UPDATE catalog_entries SET body=json_set(body, '$.default_checked', json('true'))
               WHERE run_id=?
                 AND json_extract(body, '$.verdict') IN ('JOIN', 'SPEC_NODE', 'GROUP', 'NEW_NODE')
                 AND COALESCE(json_extract(body, '$.default_checked'), 0) = 0
                 AND COALESCE(json_extract(body, '$.contested'), 0) = 0
                 AND json_extract(body, '$.override') IS NULL
                 AND (json_extract(body, '$.sensitivity') = 'L1'
                      OR (json_extract(body, '$.sensitivity') = 'L2' AND json_extract(body, '$.kind') = 'image'))""",
            (run_id,))
        if cur.rowcount:
            self.conn.commit()
        return cur.rowcount

    def approve(self, run_id: str, entry_ids: list[str]) -> None:
        """승인: 기본 선택으로 바꿔 정리 예정에 넣는다."""
        self.conn.executemany(
            "UPDATE catalog_entries SET body=json_set(body, '$.default_checked', json('true')) WHERE run_id=? AND entry_id=?",
            [(run_id, i) for i in entry_ids])
        self.conn.commit()

    def effective_selection(self, run_id: str, entries: list[dict] | None = None) -> set[str]:
        """사용자가 바꾼 체크 + 바꾸지 않은 항목의 기본 체크."""
        entries = entries if entries is not None else self.entries(run_id)
        m = self.checked_map(run_id)
        return {e["entry_id"] for e in entries
                if e["verdict"] in self.MOVABLE and m.get(e["entry_id"], bool(e.get("default_checked")))}

    def set_override(self, run_id: str, entry_ids: list[str], target: str) -> None:
        if not entry_ids:
            return
        clearing = target in ("CLEAR", "LEAVE")
        if clearing:
            self._sel_table()
        with self.conn:
            if clearing:
                self.conn.executemany("DELETE FROM catalog_overrides WHERE run_id=? AND entry_id=?",
                                      [(run_id, eid) for eid in entry_ids])
                self.conn.executemany("INSERT OR REPLACE INTO catalog_selection VALUES(?,?,0)",
                                      [(run_id, eid) for eid in entry_ids])
            else:
                now = now_iso()
                self.conn.executemany("INSERT OR REPLACE INTO catalog_overrides VALUES(?,?,?,?)",
                                      [(run_id, eid, target, now) for eid in entry_ids])
            self._advance_review(run_id)

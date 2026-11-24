"""작업별 파일 목록을 SQLite에 보관하고 현재 묶음만 파일 시스템과 대조한다."""

from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import dataclass, replace
from pathlib import Path

from tongdory2.fsx.paths import is_link_stat
from tongdory2.fsx.roots import Role
from tongdory2.fsx.scan import ScanCoverage, ScanItem, collect_boundary, scan_collect
from tongdory2.fsx.scope import Access, Scope

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory_lists(
    series_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, completed INTEGER NOT NULL,
    limited INTEGER NOT NULL, errors INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory_items(
    series_id TEXT NOT NULL, path TEXT NOT NULL, root_id TEXT NOT NULL, role TEXT NOT NULL,
    rel TEXT NOT NULL, is_dir INTEGER NOT NULL, is_link INTEGER NOT NULL,
    size INTEGER NOT NULL, mtime REAL NOT NULL, mtime_ns INTEGER NOT NULL,
    device INTEGER NOT NULL, inode INTEGER NOT NULL, ctime_ns INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN('PENDING','DONE','SKIPPED')),
    present INTEGER NOT NULL,
    PRIMARY KEY(series_id,path)
);
CREATE INDEX IF NOT EXISTS inventory_pending ON inventory_items(series_id,mtime_ns DESC,path DESC)
    WHERE state='PENDING' AND present=1;
"""
_COLUMNS = """path,root_id,role,rel,is_dir,is_link,size,mtime,mtime_ns,device,inode,ctime_ns"""
_STAGE = """
CREATE TEMP TABLE inventory_scan(
    path TEXT PRIMARY KEY, root_id TEXT, role TEXT, rel TEXT, is_dir INTEGER, is_link INTEGER,
    size INTEGER, mtime REAL, mtime_ns INTEGER, device INTEGER, inode INTEGER, ctime_ns INTEGER,
    state TEXT
);
"""


def _metadata(item: ScanItem) -> tuple:
    return (str(item.path), item.root_id, item.role.value, item.rel, item.is_dir, item.is_link,
            item.size, item.mtime, item.mtime_ns, item.device, item.inode, item.ctime_ns)


def _item(row) -> ScanItem:
    path, root_id, role, rel, is_dir, is_link, size, mtime, mtime_ns, device, inode, ctime_ns = row
    return ScanItem(Path(path), root_id, Role(role), rel, bool(is_dir), size, mtime, bool(is_link), mtime_ns,
                    device, inode, ctime_ns)


class InventoryCheck:
    """폴더 정책을 묶음에서 재사용하고, 파일 경로와 지문은 매번 검증한다."""

    def __init__(self, scope: Scope) -> None:
        self.scope = scope
        self.directories: dict[Path, bool] = {}

    def current(self, item: ScanItem) -> ScanItem | None:
        check = self.scope.check(item.path.parent if item.is_link else item.path, Access.READ)
        if not check.allowed or check.root is None or check.root.root_id != item.root_id:
            return None
        for parent in item.path.parents:
            if parent == check.root.path:
                break
            if parent not in self.directories:
                self.directories[parent] = collect_boundary(parent)
            if self.directories[parent]:
                return None
        try:
            st = item.path.lstat()
        except OSError:
            return None
        if item.is_link:
            expected_type = is_link_stat(st)
        elif item.is_dir:
            expected_type = stat.S_ISDIR(st.st_mode) and not is_link_stat(st) and collect_boundary(item.path)
        else:
            expected_type = stat.S_ISREG(st.st_mode)
        if not expected_type:
            return None
        return replace(item, size=0 if item.is_dir else st.st_size, mtime=st.st_mtime, mtime_ns=st.st_mtime_ns,
                       device=st.st_dev, inode=st.st_ino, ctime_ns=st.st_ctime_ns)


@dataclass
class Selection:
    items: list[ScanItem]
    coverage: ScanCoverage
    completed: int
    skipped: int


class InventoryStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        conn.executescript(_SCHEMA)

    def exists(self, series_id: str, scope_key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM inventory_lists WHERE series_id=? AND scope_key=?",
                                 (series_id, scope_key)).fetchone() is not None

    def unapproved(self, scope_key: str) -> str | None:
        row = self.conn.execute("""SELECT series_id FROM inventory_lists
                                 WHERE scope_key=? AND completed=0 ORDER BY rowid DESC LIMIT 1""",
                                (scope_key,)).fetchone()
        return row[0] if row else None

    def pending_count(self, series_id: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM inventory_items WHERE series_id=? AND state='PENDING' AND present=1",
                                 (series_id,)).fetchone()[0]

    def rebuild(self, series_id: str, scope_key: str, roots, scope: Scope, include_current: bool, *,
                progress, legacy: set[tuple[str, int, int]] | None = None,
                items: list[ScanItem] | None = None, keep=None, stage=None) -> int:
        """items가 있으면 폴더를 다시 훑지 않고 그 목록을 쓴다(파일 종류 살펴보기 결과).
        keep(item)이 False인 항목은 DB에 저장하지 않는다(사용자가 제외한 확장자·폴더)."""
        coverage = ScanCoverage()
        legacy = legacy or set()
        pending_rows: list[tuple] = []
        self.conn.execute("DROP TABLE IF EXISTS temp.inventory_scan")
        self.conn.execute(_STAGE)

        def flush() -> None:
            self.conn.executemany("INSERT OR IGNORE INTO temp.inventory_scan VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  pending_rows)
            pending_rows.clear()

        saved = [0]

        def record(item: ScanItem) -> None:
            if keep is not None and not keep(item):
                return
            state = "DONE" if (str(item.path), item.size, item.mtime_ns) in legacy else "PENDING"
            pending_rows.append((*_metadata(item), state))
            saved[0] += 1
            if len(pending_rows) == 1_000:
                flush()
                if stage and items is not None:
                    stage("목록 저장", saved[0], 0)

        try:
            # 탐색 중에는 임시 테이블만 작성한다. 다른 연결은 기존 상태를 계속 조회할 수 있다.
            with self.conn:
                if items is None:
                    scan_collect(roots, scope, include_current=include_current, coverage=coverage,
                                 progress=progress, on_item=record)
                else:
                    for it in items:
                        record(it)
                flush()
            if stage:
                stage("목록 정리", saved[0], 0)
            with self.conn:
                self.conn.execute(
                    """INSERT INTO inventory_lists VALUES(?,?,?,?,?)
                       ON CONFLICT(series_id) DO UPDATE SET limited=excluded.limited, errors=excluded.errors""",
                    (series_id, scope_key, len(legacy), coverage.limited, coverage.errors),
                )
                self.conn.execute("UPDATE inventory_items SET present=0 WHERE series_id=?", (series_id,))
                self.conn.execute(f"""
                    INSERT INTO inventory_items(series_id,{_COLUMNS},state,present)
                    SELECT ?,{_COLUMNS},state,1 FROM temp.inventory_scan WHERE 1
                    ON CONFLICT(series_id,path) DO UPDATE SET
                        state=CASE WHEN inventory_items.state='DONE'
                            AND inventory_items.device=excluded.device AND inventory_items.inode=excluded.inode
                            AND inventory_items.ctime_ns=excluded.ctime_ns AND inventory_items.mtime_ns=excluded.mtime_ns
                            AND inventory_items.size=excluded.size THEN 'DONE' ELSE excluded.state END,
                        root_id=excluded.root_id,role=excluded.role,rel=excluded.rel,is_dir=excluded.is_dir,
                        is_link=excluded.is_link,size=excluded.size,mtime=excluded.mtime,mtime_ns=excluded.mtime_ns,
                        device=excluded.device,inode=excluded.inode,ctime_ns=excluded.ctime_ns,present=1
                    """, (series_id,))
                self.conn.execute("""UPDATE inventory_items SET state='SKIPPED'
                                     WHERE series_id=? AND present=0 AND state='PENDING'""", (series_id,))
        finally:
            self.conn.execute("DROP TABLE temp.inventory_scan")
        return saved[0]

    def select(self, series_id: str, scope: Scope, limit: int, stage=None) -> Selection:
        check = InventoryCheck(scope)
        items: list[ScanItem] = []
        cursor = None
        updates = []
        while len(items) < limit:
            if cursor is not None:
                # 이미 고른 항목은 아직 PENDING 상태다. 없어진 항목을 보충할 때 정렬 커서로 건너뛴다.
                condition = "AND (mtime_ns,path) < (?,?)"
                params = (series_id, *cursor, limit - len(items))
            else:
                condition = ""
                params = (series_id, limit)
            rows = self.conn.execute(f"""
                SELECT {_COLUMNS} FROM inventory_items WHERE series_id=? AND state='PENDING' AND present=1
                {condition} ORDER BY mtime_ns DESC,path DESC LIMIT ?
                """, params).fetchall()
            if not rows:
                break
            skipped = []
            for row in rows:
                if stage and len(items) % 200 == 0:
                    stage("이번 묶음 확인", len(items), limit)
                saved = _item(row)
                cursor = (saved.mtime_ns, str(saved.path))
                current = check.current(saved)
                if current is None:
                    skipped.append((series_id, str(saved.path)))
                    continue
                items.append(current)
                if current != saved:
                    updates.append((current.size, current.mtime, current.mtime_ns, current.device,
                                    current.inode, current.ctime_ns, series_id, str(current.path)))
            with self.conn:
                self.conn.executemany("""UPDATE inventory_items SET state='SKIPPED',present=0
                                        WHERE series_id=? AND path=?""", skipped)
        with self.conn:
            self.conn.executemany("""UPDATE inventory_items SET size=?,mtime=?,mtime_ns=?,device=?,inode=?,ctime_ns=?
                                    WHERE series_id=? AND path=?""", updates)
        completed, limited, errors = self.conn.execute(
            "SELECT completed,limited,errors FROM inventory_lists WHERE series_id=?", (series_id,),
        ).fetchone()
        found, pending, skipped_count = self.conn.execute(
            """SELECT COUNT(CASE WHEN present=1 THEN 1 END),
                      COUNT(CASE WHEN state='PENDING' AND present=1 THEN 1 END),
                      COUNT(CASE WHEN state='SKIPPED' THEN 1 END)
               FROM inventory_items WHERE series_id=?""", (series_id,),
        ).fetchone()
        return Selection(items, ScanCoverage(bool(limited), errors, found, pending), completed, skipped_count)


def completed_item(conn: sqlite3.Connection, series_id: str, path: str) -> ScanItem | None:
    row = conn.execute(f"""SELECT {_COLUMNS} FROM inventory_items
                           WHERE series_id=? AND path=? AND state='DONE' AND present=1""",
                       (series_id, path)).fetchone()
    return _item(row) if row else None


def complete_selection(conn: sqlite3.Connection, series_id: str, items: list[ScanItem]) -> None:
    """호출자가 분석 결과를 저장하는 트랜잭션 안에서 묶음 상태를 반영한다."""
    updated = conn.executemany(
        """UPDATE inventory_items SET state='DONE'
           WHERE series_id=? AND path=? AND state='PENDING' AND device=? AND inode=? AND ctime_ns=? AND mtime_ns=?""",
        [(series_id, str(item.path), item.device, item.inode, item.ctime_ns, item.mtime_ns) for item in items],
    ).rowcount
    if updated != len(items):
        raise RuntimeError("저장할 분석 결과와 파일 목록이 일치하지 않습니다.")
    conn.execute("UPDATE inventory_lists SET completed=completed+? WHERE series_id=?", (updated, series_id))
    for previous, in conn.execute("SELECT series_id FROM inventory_lists WHERE series_id<>?", (series_id,)).fetchall():
        conn.execute("DELETE FROM inventory_items WHERE series_id=?", (previous,))
        conn.execute("DELETE FROM inventory_lists WHERE series_id=?", (previous,))

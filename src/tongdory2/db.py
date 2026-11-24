"""SQLite 저장소. 스키마는 단계별로 늘린다 (지금: 등록 루트)."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from tongdory2.fsx.roots import Code, RegisteredRoot, Role, RootCheck

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS roots(
  root_id TEXT PRIMARY KEY,
  role TEXT NOT NULL CHECK(role IN('BASELINE','CURRENT','COLLECT','IN_PLACE','STORAGE')),
  abs_path TEXT NOT NULL UNIQUE,
  origin TEXT CHECK(origin IN('DISCOVERED','PASTE','BROWSER')),
  linked_root_id TEXT REFERENCES roots(root_id) ON DELETE SET NULL,
  check_codes TEXT NOT NULL DEFAULT '[]',
  acks TEXT NOT NULL DEFAULT '[]',
  confirmed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  confirmed_at TEXT,
  CHECK(confirmed=0 OR confirmed_at IS NOT NULL)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


class DB:
    """스레드마다 자기 SQLite 연결을 쓴다 (웹 요청은 스레드 풀에서 동시에 실행됨)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._local = threading.local()
        self._opened: list[sqlite3.Connection] = []  # close_all()에서 다른 스레드의 연결도 닫기 위해 기록
        self._opened_lock = threading.Lock()
        self.conn.executescript(SCHEMA)

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            # 각 연결은 만든 스레드만 쓴다. check_same_thread=False는 종료 시 close_all()에서 닫기 위한 것.
            c = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=10000")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
            with self._opened_lock:
                self._opened.append(c)
        return c

    def close_all(self) -> None:
        """이 DB로 연 모든 스레드의 연결을 닫는다 (Windows는 열린 DB 파일을 지울 수 없다)."""
        with self._opened_lock:
            opened, self._opened = self._opened, []
        for c in opened:
            try:
                c.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    # ---------------- roots ----------------
    def list_roots(self) -> list[RegisteredRoot]:
        rows = self.conn.execute("SELECT * FROM roots ORDER BY created_at, root_id").fetchall()
        return [self._row_to_root(r) for r in rows]

    def get_root(self, root_id: str) -> RegisteredRoot | None:
        r = self.conn.execute("SELECT * FROM roots WHERE root_id=?", (root_id,)).fetchone()
        return self._row_to_root(r) if r else None

    def add_root(
        self, path: Path, role: Role, origin: str, codes: list[Code], acks: list[Code]
    ) -> RegisteredRoot:
        rid = new_id("root")
        with self.conn:
            self.conn.execute(
                "INSERT INTO roots(root_id, role, abs_path, origin, check_codes, acks, created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (rid, role.value, str(path), origin, json.dumps([c.value for c in codes]),
                 json.dumps([a.value for a in acks]), now_iso()),
            )
        return self.get_root(rid)  # type: ignore[return-value]

    def remove_root(self, root_id: str) -> None:
        """등록 해제는 DB만 바꾼다 (R-16.9, 파일 불변)."""
        with self.conn:
            self.conn.execute("DELETE FROM roots WHERE root_id=?", (root_id,))

    def set_link(self, root_id: str, linked_root_id: str | None) -> None:
        with self.conn:
            self.conn.execute("UPDATE roots SET linked_root_id=? WHERE root_id=?", (linked_root_id, root_id))

    def confirm_all(self) -> int:
        with self.conn:
            cur = self.conn.execute(
                "UPDATE roots SET confirmed=1, confirmed_at=? WHERE confirmed=0", (now_iso(),)
            )
        return cur.rowcount

    def unconfirm_all(self) -> None:
        with self.conn:
            self.conn.execute("UPDATE roots SET confirmed=0, confirmed_at=NULL")

    def restart_setup(self) -> None:
        """세 단계 설정을 모두 비워 처음부터 다시 시작하고 이전 구조·실행 이력은 보관한다."""
        with self.conn:
            self.conn.execute("UPDATE spec_versions SET status='SUPERSEDED' WHERE status='CONFIRMED'")
            self.conn.execute("DELETE FROM roots WHERE role IN ('COLLECT','CURRENT','IN_PLACE')")
            self.conn.execute("UPDATE roots SET confirmed=0, confirmed_at=NULL WHERE role='STORAGE'")

    def save_setup_roots(self, role: Role, checks: list[RootCheck], storage: RootCheck | None = None) -> None:
        """설정 화면에서 바꾼 역할만 한 번에 저장하고, 그대로인 경로의 ID는 유지한다."""
        wanted = {check.realpath for check in checks}
        roots = self.list_roots()
        existing = {r.path: r for r in roots if r.role == role}
        current_storage = next((r for r in roots if r.role == Role.STORAGE and storage and r.path == storage.realpath), None)
        now = now_iso()
        with self.conn:
            for path, root in existing.items():
                if path not in wanted:
                    self.conn.execute("DELETE FROM roots WHERE root_id=?", (root.root_id,))
            for item_role, check in [(role, c) for c in checks] + ([(Role.STORAGE, storage)] if storage else []):
                registered = existing.get(check.realpath) if item_role == role else current_storage
                if registered:
                    self.conn.execute("UPDATE roots SET confirmed=1, confirmed_at=?, check_codes=?, acks=? WHERE root_id=?",
                                      (now, json.dumps([c.value for c in check.codes]),
                                       json.dumps([c.value for c in check.warn_codes]), registered.root_id))
                    continue
                self.conn.execute(
                    "INSERT INTO roots(root_id, role, abs_path, origin, check_codes, acks, confirmed, created_at, confirmed_at)"
                    " VALUES(?,?,?,?,?,?,1,?,?)",
                    (new_id("root"), item_role.value, str(check.realpath), "PASTE",
                     json.dumps([c.value for c in check.codes]), json.dumps([c.value for c in check.warn_codes]), now, now),
                )

    @staticmethod
    def _row_to_root(r: sqlite3.Row) -> RegisteredRoot:
        return RegisteredRoot(
            root_id=r["root_id"],
            path=Path(r["abs_path"]),
            role=Role(r["role"]),
            confirmed=bool(r["confirmed"]),
            acks=[Code(a) for a in json.loads(r["acks"])],
            linked_root_id=r["linked_root_id"],
        )

"""계획 실행과 배치 단위 Undo.

- 이동은 같은 볼륨에서 `os.link` + `os.unlink`: 목적지가 이미 있으면 link가 실패하므로 덮어쓰기가 원천 차단된다.
- 실행 전 작업을 먼저 기록(write-ahead)하고, 끝나면 상태를 갱신한다.
- Undo는 역순으로 되돌리고, 이번 배치가 만든 빈 폴더만 지운다(rmdir). 그사이 바뀐 파일은 건드리지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from tempfile import NamedTemporaryFile
from pathlib import Path

from tongdory2.db import new_id, now_iso
from tongdory2.fsx.scope import Access, Scope
from tongdory2.fsx.roots import in_git_repo
from tongdory2.fsx.sensitivity import HEAD_BYTES
from tongdory2.plan.safety import Decision, GateContext, Op, PlanOp, gate

log = logging.getLogger(__name__)
_WINDOWS = os.name == "nt"

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans(plan_id TEXT PRIMARY KEY, run_id TEXT, status TEXT NOT NULL, body TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batches(batch_id TEXT PRIMARY KEY, plan_id TEXT, status TEXT NOT NULL, summary TEXT,
  created_at TEXT NOT NULL, undone_at TEXT);
CREATE TABLE IF NOT EXISTS operations(batch_id TEXT NOT NULL, seq INTEGER NOT NULL, op TEXT NOT NULL, src TEXT, dst TEXT NOT NULL,
  status TEXT NOT NULL, reason TEXT, created_dir INTEGER DEFAULT 0, after_fp TEXT, undo_status TEXT,
  PRIMARY KEY(batch_id, seq));
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def save_plan(conn: sqlite3.Connection, run_id: str, body: dict) -> str:
    init(conn)
    pid = new_id("plan")
    with conn:
        conn.execute("INSERT INTO plans VALUES(?,?,?,?,?)", (pid, run_id, "READY", json.dumps(body, ensure_ascii=False), now_iso()))
    return pid


def load_plan(conn: sqlite3.Connection, plan_id: str) -> dict | None:
    init(conn)
    r = conn.execute("SELECT status, body, run_id FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
    return {"status": r[0], "body": json.loads(r[1]), "run_id": r[2]} if r else None


@dataclass
class ExecResult:
    batch_id: str
    moved: int = 0
    moved_by_kind: dict[str, int] = field(default_factory=dict)
    created_dirs: int = 0
    skipped: list[dict] = field(default_factory=list)
    denied: list[dict] = field(default_factory=list)
    both_paths: int = 0  # #49 원본 제거·목적지 회수 모두 실패해 두 경로가 남은 수


def _disk_name(p: Path) -> Path:
    """#71 디스크에 실제로 저장된 이름(바이트 그대로)을 찾는다. 화면·검사는 NFC로 다루지만 macOS 파일 이름은
    NFD일 수 있어, NFC 이름으로 새 링크를 만들면 옮기거나 되돌린 뒤 이름 표현이 달라진다."""
    import unicodedata

    want = unicodedata.normalize("NFC", p.name)
    try:
        for name in os.listdir(p.parent):
            if name == p.name:
                return p
            if unicodedata.normalize("NFC", name) == want:
                return p.parent / name
    except OSError:
        pass
    return p


def _fp(p: Path) -> dict:
    st = os.lstat(p)
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _move_fp(path: Path) -> dict:
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode):
        raise OSError("일반 파일이 아닙니다")
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "dev": st.st_dev, "ino": st.st_ino}


def _matches_move(path: Path, fingerprint: dict) -> bool:
    try:
        current = _move_fp(path)
    except OSError:
        return False
    return all(current.get(key) == value for key, value in fingerprint.items())


def execute(conn: sqlite3.Connection, plan_id: str, ctx: GateContext) -> ExecResult:
    init(conn)
    plan = load_plan(conn, plan_id)
    if not plan or plan["status"] != "READY":
        raise ValueError("실행할 수 없는 계획입니다 (이미 실행했거나 없음)")
    body = plan["body"]
    if body.get("settings_key") != ctx.settings_key:
        raise ValueError("정리 설정이 바뀌었거나 확인할 수 없습니다. 새 계획을 만들어 주세요.")
    ops = [PlanOp(**(o | {"op": Op(o["op"])})) for o in body["ops"]]
    ctx.planned_mkdirs = set(body["mkdirs"])
    ctx.allowed_dest_dirs = set(body["allowed_dest_dirs"])
    # 저장소 여부는 계획 본문을 믿지 않고 실행 시점에 다시 확인한다
    ctx.is_git_repo = in_git_repo(ctx.current_root, ctx.scope.home)
    ctx.now = time.time()
    bid = new_id("batch")
    res = ExecResult(bid)
    with conn:  # 계획을 먼저 소진 처리 — 같은 계획 두 번 실행 방지
        cur = conn.execute("UPDATE plans SET status='EXECUTED' WHERE plan_id=? AND status='READY'", (plan_id,))
        if cur.rowcount != 1:
            raise ValueError("이미 실행 중이거나 실행된 계획입니다")
        conn.execute("INSERT INTO batches VALUES(?,?,?,?,?,NULL)", (bid, plan_id, "RUNNING", None, now_iso()))

    for o in ops:
        with conn:
            conn.execute("INSERT INTO operations(batch_id, seq, op, src, dst, status) VALUES(?,?,?,?,?,?)",
                         (bid, o.seq, o.op.value, o.src, o.dst, "PENDING"))
        g = gate(o, ctx)
        status, reason, created = "DONE", g.reason, 0
        after = None
        both = False
        if g.decision != Decision.ALLOW:
            status = g.decision.value
            (res.denied if g.decision == Decision.DENY else res.skipped).append(
                {"seq": o.seq, "name": Path(o.src or o.dst).name, "reason": g.reason, "detail": g.detail})
        else:
            try:
                if o.op == Op.MKDIR:
                    try:
                        os.mkdir(o.dst)
                        created = 1
                        res.created_dirs += 1
                    except FileExistsError:
                        created = 0
                else:  # MOVE / TRASH
                    raw = _disk_name(Path(o.src))
                    if raw.name != Path(o.src).name:  # 원래 이름 표현(NFD 등)을 그대로 유지
                        same_name = Path(o.dst).name == Path(o.src).name  # 휴지통에서 _1을 붙인 경우는 그대로
                        o.src = str(raw)
                        if same_name:
                            o.dst = str(Path(o.dst).parent / raw.name)
                        with conn:
                            conn.execute("UPDATE operations SET src=?, dst=? WHERE batch_id=? AND seq=?",
                                         (o.src, o.dst, bid, o.seq))
                    if o.op == Op.TRASH and Path(o.dst).parent == ctx.scope.home / ".Trash":
                        os.makedirs(Path(o.dst).parent, exist_ok=True)  # #73 휴지통 폴더가 없는 홈(새 계정·시험 홈)
                    # 목적지 링크를 만들기 전에 원본의 식별 정보를 저장한다.
                    before = _move_fp(Path(o.src))
                    with conn:
                        conn.execute("UPDATE operations SET after_fp=? WHERE batch_id=? AND seq=?",
                                     (json.dumps(before), bid, o.seq))
                    os.link(o.src, o.dst)  # 목적지가 있으면 FileExistsError → 덮어쓰기 불가
                    try:
                        os.unlink(o.src)
                    except OSError:
                        # #49 원본 경로를 지우지 못함 → 방금 만든 목적지 링크를 거둬 원래 상태로(같은 파일이라 내용 손실 없음)
                        try:
                            os.unlink(o.dst)
                        except OSError:
                            both = True  # 두 경로가 남음 — 되돌리기에서 목적지 링크만 정리
                        raise
                    after = json.dumps(before)
                    res.moved += 1
                    res.moved_by_kind[o.kind] = res.moved_by_kind.get(o.kind, 0) + 1
            except FileExistsError:
                status, reason = "SKIP", "NAME_CONFLICT"
                res.skipped.append({"seq": o.seq, "name": Path(o.src or o.dst).name, "reason": reason, "detail": ""})
            except OSError as exc:
                status, reason = "FAILED", ("BOTH_PATHS:" if both else "") + type(exc).__name__
                res.both_paths += both
                res.skipped.append({"seq": o.seq, "name": Path(o.src or o.dst).name, "reason": reason, "detail": str(exc)[:120]})
        with conn:
            conn.execute("UPDATE operations SET status=?, reason=?, created_dir=?, after_fp=? WHERE batch_id=? AND seq=?",
                         (status, reason, created, after, bid, o.seq))

    summary = {"both_paths": res.both_paths, "moved": res.moved, "moved_by_kind": res.moved_by_kind, "created_dirs": res.created_dirs, "skipped": len(res.skipped), "denied": len(res.denied)}
    with conn:
        conn.execute("UPDATE batches SET status=?, summary=? WHERE batch_id=?",
                     ("DONE" if (res.moved or res.created_dirs or res.both_paths) else "EMPTY", json.dumps(summary, ensure_ascii=False), bid))
    return res


def complete_review_only(conn: sqlite3.Connection, plan_id: str, settings_key: str) -> ExecResult:
    """R02: 옮길 파일이 0개인 계획을 명시적으로 '이동 없이 검토 완료' 처리한다."""
    init(conn)
    plan = load_plan(conn, plan_id)
    if not plan or plan["status"] != "READY":
        raise ValueError("실행할 수 없는 계획입니다 (이미 실행했거나 없음)")
    if plan["body"].get("settings_key") != settings_key:
        raise ValueError("정리 설정이 바뀌었거나 확인할 수 없습니다. 새 계획을 만들어 주세요.")
    bid = new_id("batch")
    summary = {"both_paths": 0, "moved": 0, "moved_by_kind": {}, "created_dirs": 0, "skipped": 0, "denied": 0, "review_only": True}
    with conn:
        cur = conn.execute("UPDATE plans SET status='EXECUTED' WHERE plan_id=? AND status='READY'", (plan_id,))
        if cur.rowcount != 1:
            raise ValueError("이미 실행 중이거나 실행된 계획입니다")
        conn.execute("INSERT INTO batches VALUES(?,?,?,?,?,NULL)",
                     (bid, plan_id, "EMPTY", json.dumps(summary, ensure_ascii=False), now_iso()))
    return ExecResult(bid)


@dataclass
class UndoResult:
    batch_id: str
    restored: int = 0
    trashed: int = 0  # 복사본·새 파일을 휴지통으로 (원위치가 아님)
    removed_dirs: int = 0
    stopped: list[dict] = field(default_factory=list)


def _reconcile_pending(conn: sqlite3.Connection, res: UndoResult, scope: Scope) -> None:
    """중단된 작업에서 같은 파일임을 확인할 수 있을 때만 되돌리기 대상으로 복구한다."""
    rows = conn.execute(
        "SELECT seq, op, src, dst, after_fp FROM operations WHERE batch_id=? AND status='PENDING'",
        (res.batch_id,),
    ).fetchall()
    for seq, op, src, dst, stored in rows:
        destination = Path(dst)
        status = None
        if op in ("MOVE", "TRASH") and stored:
            fingerprint = json.loads(stored)
            source = Path(src)
            access = Access.TRASH_DST if op == "TRASH" else Access.MOVE_SRC
            if scope.check(source, Access.RESTORE_DST).allowed and scope.check(destination, access).allowed:
                if _matches_move(destination, fingerprint):
                    status = "DONE"
                elif _matches_move(source, fingerprint):
                    status = "SKIP"
        elif op == "MKDIR" and scope.check(destination, Access.MKDIR).allowed:
            if not destination.exists() and not destination.is_symlink():
                status = "SKIP"
        if status is None:
            # 생성 여부를 입증할 수 없는 폴더나 예전 기록은 사용자가 확인할 수 있게 남긴다.
            res.stopped.append({"seq": seq, "name": destination.name, "reason": "INTERRUPTED_UNCERTAIN"})
            with conn:
                conn.execute("UPDATE operations SET undo_status=? WHERE batch_id=? AND seq=?",
                             ("INTERRUPTED_UNCERTAIN", res.batch_id, seq))
        else:
            with conn:
                conn.execute("UPDATE operations SET status=?, reason='INTERRUPTED', undo_status=NULL "
                             "WHERE batch_id=? AND seq=?", (status, res.batch_id, seq))


def undo(conn: sqlite3.Connection, batch_id: str, scope: Scope) -> UndoResult:
    init(conn)
    b = conn.execute("SELECT status FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    if not b or b[0] not in ("DONE", "PARTIAL_UNDO", "RUNNING"):
        raise ValueError("되돌릴 수 없는 배치입니다")
    res = UndoResult(batch_id)
    _reconcile_pending(conn, res, scope)
    rows = conn.execute(
        "SELECT seq, op, src, dst, created_dir, after_fp FROM operations WHERE batch_id=? AND status='DONE'"
        " AND (undo_status IS NULL OR undo_status!='UNDONE') ORDER BY seq DESC", (batch_id,)).fetchall()
    # #49 원본 제거에 실패해 두 경로가 남은 이동: 같은 파일(같은 inode)일 때만 목적지 링크를 거둔다
    for seq, src, dst in conn.execute(
            "SELECT seq, src, dst FROM operations WHERE batch_id=? AND status='FAILED' AND reason LIKE 'BOTH_PATHS:%'"
            " AND (undo_status IS NULL OR undo_status!='UNDONE')", (batch_id,)).fetchall():
        s, d = Path(src), Path(dst)
        status = "UNDONE"
        try:
            if not d.exists():
                pass  # 이미 정리됨
            elif not s.exists() or not os.path.samefile(s, d):
                status = "CHANGED_AFTER_BATCH"
            elif not scope.check(d, Access.MOVE_SRC).allowed:
                status = "OUT_OF_SCOPE"
            else:
                os.unlink(d)
                res.restored += 1
        except OSError as exc:
            status = f"FAILED:{type(exc).__name__}"
        if status != "UNDONE":
            res.stopped.append({"seq": seq, "name": d.name, "reason": status})
        with conn:
            conn.execute("UPDATE operations SET undo_status=? WHERE batch_id=? AND seq=?", (status, batch_id, seq))
    for seq, op, src, dst, created_dir, after_fp in rows:
        status = "UNDONE"
        if op in ("MOVE", "TRASH"):
            d, s = Path(dst), Path(src)
            if not d.exists():
                status = "MISSING"
            elif after_fp and not _matches_move(d, json.loads(after_fp)):
                status = "CHANGED_AFTER_BATCH"
            elif not scope.check(s, Access.RESTORE_DST).allowed or not scope.check(d, Access.TRASH_DST if op == "TRASH" else Access.MOVE_SRC).allowed:
                status = "OUT_OF_SCOPE"
            else:
                try:
                    # 이전 Undo가 링크 생성 후 멈췄다면 같은 파일인 경우에만 마무리한다.
                    if s.exists() and not s.is_symlink() and os.path.samefile(d, s):
                        os.unlink(d)
                    else:
                        os.link(d, s)  # 다른 파일이 원위치에 있으면 덮어쓰지 않음
                        try:
                            os.unlink(d)
                        except OSError:
                            try:
                                os.unlink(s)  # 이번에 만든 링크를 회수해 재시도 가능하게 유지
                            except OSError:
                                pass  # 두 경로가 남아도 다음 Undo가 samefile 검사 후 마무리
                            raise
                    res.restored += 1
                except FileExistsError:
                    status = "ORIGINAL_OCCUPIED"
                except OSError as exc:
                    status = f"FAILED:{type(exc).__name__}"
        elif op in ("WRITE_NEW", "COPY"):
            d = Path(dst)
            trash = scope.home / ".Trash" / d.name
            j = 1
            reuse_trash = False
            while trash.exists() or trash.is_symlink():
                if not trash.is_symlink() and d.exists() and os.path.samefile(d, trash):
                    reuse_trash = True
                    break  # 이전 Undo에서 삭제만 실패한 사본의 휴지통 링크 재사용
                trash = trash.with_name(f"{d.stem}_{j}{d.suffix}")
                j += 1
            if not d.exists():
                status = "MISSING"
            elif after_fp and _fp(d) != json.loads(after_fp):
                status = "CHANGED_AFTER_BATCH"  # 사용자가 고친 색인은 그대로 둔다
            else:
                try:
                    trash.parent.mkdir(exist_ok=True)
                    if not reuse_trash:
                        os.link(d, trash)  # 삭제 대신 휴지통으로
                    try:
                        os.unlink(d)
                    except OSError:
                        try:
                            os.unlink(trash)
                        except OSError:
                            pass  # 남은 링크는 다음 Undo에서 재사용
                        raise
                    res.trashed += 1
                except OSError as exc:
                    status = f"FAILED:{type(exc).__name__}"
        elif op == "MKDIR":
            if not created_dir:
                status = "UNDONE"  # 원래 있던 폴더 — 건드리지 않음
            else:
                p = Path(dst)
                try:
                    if p.is_dir() and not any(p.iterdir()) and scope.check(p, Access.MKDIR).allowed:
                        os.rmdir(p)
                        res.removed_dirs += 1
                    else:
                        status = "NOT_EMPTY"
                except OSError:
                    status = "NOT_EMPTY"
        if status not in ("UNDONE",):
            res.stopped.append({"seq": seq, "name": Path(dst).name, "reason": status})
        with conn:
            conn.execute("UPDATE operations SET undo_status=? WHERE batch_id=? AND seq=?", (status, batch_id, seq))
    with conn:
        conn.execute("UPDATE batches SET status=?, undone_at=? WHERE batch_id=?",
                     ("UNDONE" if not res.stopped else "PARTIAL_UNDO", now_iso(), batch_id))
    return res


def write_new(conn: sqlite3.Connection, path: Path, content: str, scope: Scope, home: Path, label: str) -> tuple[str, Path]:
    """새 파일 쓰기 (색인·인수인계 README). 기존 파일은 덮어쓰지 않고 `_1` 등으로 비켜 간다. 배치로 기록 → Undo 시 휴지통."""
    init(conn)
    target = path
    i = 1
    while target.exists() or target.is_symlink():
        target = path.with_name(f"{path.stem}_{i}{path.suffix}")
        i += 1
    r = scope.check(target, Access.WRITE_NEW)
    if not r.allowed:
        raise PermissionError(f"쓰기 범위 밖: {r.reason.value}")
    data = content.encode("utf-8")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_BINARY", 0), 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    bid = new_id("batch")
    with conn:
        conn.execute("INSERT INTO batches VALUES(?,?,?,?,?,NULL)", (bid, None, "DONE",
                     json.dumps({"moved": 0, "created_dirs": 0, "written": 1, "label": label}, ensure_ascii=False), now_iso()))
        conn.execute("INSERT INTO operations(batch_id, seq, op, src, dst, status, reason, after_fp) VALUES(?,?,?,?,?,?,?,?)",
                     (bid, 1, "WRITE_NEW", None, str(target), "DONE", label, json.dumps(_fp(target))))
    return bid, target


def _publish_archive_file(temporary: Path, destination: Path) -> str:
    """닫힌 임시 파일을 같은 폴더에 공개한다. 두 방식 모두 기존 경로가 있으면 실패한다."""
    fingerprint = json.dumps(_fp(temporary))
    if _WINDOWS:
        temporary.rename(destination)
    else:
        os.link(temporary, destination)
    return fingerprint


def _discard_archive_tempfile(temporary: Path | None) -> None:
    if temporary is not None:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            log.warning("[지식센터] 임시 파일을 정리하지 못했습니다: %s", temporary, exc_info=True)


def copy_batch(conn: sqlite3.Connection, pairs: list[tuple[str, str]], mkdirs: list[str], scope: Scope, label: str,
               extra_files: dict[str, str] | Callable[[set[str]], dict[str, str]] | None = None, *,
               source_allowed: Callable[[Path, bytes], bool] | None = None,
               progress: Callable[[int, int], None] | None = None) -> tuple[str, dict]:
    """지식센터 보관용 복사. 원본은 그대로. 목적지는 ⑤ 저장 위치만(scope COPY_DST), 기존 파일 덮어쓰기 없음.
    Undo: 복사본이 그대로면 휴지통으로, 이번 배치가 만든 빈 폴더만 정리."""
    import shutil

    init(conn)
    bid = new_id("batch")
    seq = 0
    stats = {"copied": 0, "created_dirs": 0, "skipped": 0, "label": label}
    with conn:
        conn.execute("INSERT INTO batches VALUES(?,?,?,?,?,NULL)", (bid, None, "RUNNING", None, now_iso()))

    def log(op: str, src: str | None, dst: str, status: str, reason: str, created: int = 0, after: str | None = None):
        nonlocal seq
        seq += 1
        with conn:
            conn.execute("INSERT INTO operations(batch_id, seq, op, src, dst, status, reason, created_dir, after_fp)"
                         " VALUES(?,?,?,?,?,?,?,?,?)", (bid, seq, op, src, dst, status, reason, created, after))

    copied_paths: set[str] = set()
    for d in sorted(mkdirs, key=lambda x: len(Path(x).parts)):
        if not scope.check(d, Access.MKDIR).allowed:
            log("MKDIR", None, d, "DENY", "OUT_OF_SCOPE")
            continue
        try:
            os.mkdir(d)
            log("MKDIR", None, d, "DONE", "OK", 1)
            stats["created_dirs"] += 1
        except FileExistsError:
            pass
        except OSError as exc:
            log("MKDIR", None, d, "FAILED", type(exc).__name__)
    for index, (src, dst) in enumerate(pairs):
        if progress:
            progress(index, len(pairs))
        s, d = Path(src), Path(dst)
        if not scope.check(s, Access.COPY_SRC).allowed or not scope.check(d, Access.COPY_DST).allowed or s.is_symlink():
            log("COPY", src, dst, "DENY", "OUT_OF_SCOPE")
            stats["skipped"] += 1
            continue
        if d.exists():
            log("COPY", src, dst, "SKIP", "NAME_CONFLICT")
            stats["skipped"] += 1
            continue
        temporary = None
        try:
            flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
            source_fd = os.open(s, os.O_RDONLY | flags)
            with os.fdopen(source_fd, "rb") as inp:
                before = os.fstat(inp.fileno())
                if not stat.S_ISREG(before.st_mode):
                    log("COPY", src, dst, "DENY", "NOT_REGULAR_FILE")
                    stats["skipped"] += 1
                    continue
                head = inp.read(HEAD_BYTES) if source_allowed else b""
                if source_allowed and not source_allowed(s, head):
                    log("COPY", src, dst, "DENY", "SENSITIVE")
                    stats["skipped"] += 1
                    continue
                # 같은 파일 핸들에서 읽고, 완전한 사본만 최종 이름으로 공개한다.
                with NamedTemporaryFile(dir=d.parent, prefix=".tongdory-copy-", delete=False) as out:
                    temporary = Path(out.name)
                    out.write(head)
                    shutil.copyfileobj(inp, out)
                    out.flush()
                    after = os.fstat(inp.fileno())
                    if source_allowed and (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_size, after.st_mtime_ns, after.st_ctime_ns
                    ):
                        log("COPY", src, dst, "SKIP", "SOURCE_CHANGED")
                        stats["skipped"] += 1
                        continue
                    if not _WINDOWS:
                        os.fchmod(out.fileno(), stat.S_IMODE(before.st_mode) & 0o777)
                        os.utime(out.fileno(), ns=(before.st_atime_ns, before.st_mtime_ns))
                if _WINDOWS:
                    os.utime(temporary, ns=(before.st_atime_ns, before.st_mtime_ns))
                fingerprint = _publish_archive_file(temporary, d)
            log("COPY", src, dst, "DONE", "OK", 0, fingerprint)
            copied_paths.add(dst)
            stats["copied"] += 1
        except FileExistsError:
            log("COPY", src, dst, "SKIP", "NAME_CONFLICT")
            stats["skipped"] += 1
        except OSError as exc:
            log("COPY", src, dst, "FAILED", type(exc).__name__)
            stats["skipped"] += 1
        finally:
            _discard_archive_tempfile(temporary)
    extras = extra_files(copied_paths) if callable(extra_files) else (extra_files or {})
    if copied_paths:
        for name, content in extras.items():
            d = Path(name)
            if d.exists() or not scope.check(d, Access.WRITE_NEW).allowed:
                continue
            temporary = None
            try:
                with NamedTemporaryFile(dir=d.parent, prefix=".tongdory-meta-", delete=False) as out:
                    temporary = Path(out.name)
                    out.write(content.encode("utf-8"))
                fingerprint = _publish_archive_file(temporary, d)
                log("WRITE_NEW", None, str(d), "DONE", "OK", 0, fingerprint)
            except OSError as exc:
                log("WRITE_NEW", None, str(d), "FAILED", type(exc).__name__)
                stats["metadata_failed"] = True
            finally:
                _discard_archive_tempfile(temporary)
    with conn:
        conn.execute("UPDATE batches SET status=?, summary=? WHERE batch_id=?",
                     ("DONE" if stats["copied"] or stats["created_dirs"] else "EMPTY", json.dumps(stats, ensure_ascii=False), bid))
    return bid, stats


def list_batches(conn: sqlite3.Connection) -> list[dict]:
    init(conn)
    rows = conn.execute(
        "SELECT b.batch_id, b.status, b.summary, b.created_at, b.undone_at, p.run_id "
        "FROM batches b LEFT JOIN plans p ON b.plan_id=p.plan_id ORDER BY b.created_at DESC, b.rowid DESC"
    ).fetchall()
    names: dict[str, int] = {}
    batches = []
    for batch_id, status, summary, created_at, undone_at, run_id in reversed(rows):
        # 기존 기록 주소와 되돌리기 키는 유지하고, 같은 초에는 생성 순서로 표시 이름을 구분한다.
        try:
            stamp = datetime.fromisoformat(created_at).astimezone().strftime("BT_%Y%m%d_%H%M%S")
        except (ValueError, TypeError):
            stamp = batch_id
        number = names.get(stamp, 0) + 1
        names[stamp] = number
        name = stamp if number == 1 else f"{stamp}_{number:02d}"
        batches.append({"batch_id": batch_id, "name": name, "status": status, "summary": json.loads(summary) if summary else {},
                        "created_at": created_at, "undone_at": undone_at, "run_id": run_id})
    return batches[::-1]


def batch_ops(conn: sqlite3.Connection, batch_id: str) -> list[dict]:
    init(conn)
    rows = conn.execute("SELECT seq, op, src, dst, status, reason, undo_status, created_dir FROM operations WHERE batch_id=? ORDER BY seq",
                        (batch_id,)).fetchall()
    return [dict(zip(("seq", "op", "src", "dst", "status", "reason", "undo_status", "created_dir"), r)) for r in rows]

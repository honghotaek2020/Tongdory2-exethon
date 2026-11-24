"""S3 재배치: 계획·안전 게이트·실행·Undo (S-01~S-05, U-01, T-02, M-07 지정, V-02 휴지통)."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import date
from pathlib import Path

import pytest

from tongdory2.fsx.paths import real
from tongdory2.fsx.roots import RegisteredRoot, Role
from tongdory2.fsx.scope import Scope
from tongdory2.plan.executor import execute, list_batches, load_plan, save_plan, undo
from tongdory2.plan.planner import build_plan
from tongdory2.plan.safety import GateContext

OLD = time.time() - 3 * 86400

NODES = [
    {"node_id": "doc.08.01", "status": "ADOPTED", "display_path": "02 doc/08.보고/01.회의록"},
    {"node_id": "doc.05.02", "status": "ADOPTED", "display_path": "02 doc/05.구현/02.테스트데이터"},
    {"node_id": "doc.01", "status": "ADOPTED", "display_path": "02 doc/01.착수"},
]


def touch(p: Path, data: bytes = b"x", mtime: float = OLD) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(p, (mtime, mtime))
    return p


@pytest.fixture()
def env(tmp_path: Path):
    home = real(tmp_path / "home")
    desk = home / "Desktop"
    cur = home / "Documents/wsQR_2026"
    store = home / "Documents/자료보관센터"
    for d in (desk, cur / "02 doc/01.착수", cur / ".git", cur / "01 source", store / "01.보류함", home / ".Trash", home / "Pictures"):
        d.mkdir(parents=True, exist_ok=True)
    roots = [
        RegisteredRoot("r1", desk, Role.COLLECT, True),
        RegisteredRoot("r2", cur, Role.CURRENT, True),
        RegisteredRoot("r3", store, Role.STORAGE, True),
    ]
    scope = Scope(roots, home, tmp_path / "appdata")
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    files = {
        "meeting": touch(desk / "주간회의록.docx", b"meeting"),
        "quote": touch(desk / "견적서.pdf", b"quote"),
        "shot": touch(desk / "스크린샷 2026-11-10 오후 3.12.45.png", b"png"),
        "dup": touch(desk / "주간회의록(1).docx", b"meeting"),
        "hold": touch(desk / "세미나안내.pdf", b"seminar"),
        "key": touch(desk / "server.pem", b"-----BEGIN PRIVATE KEY-----"),
    }
    entries = [
        {"entry_id": "e1", "path": str(files["meeting"]), "name": files["meeting"].name, "verdict": "SPEC_NODE",
         "node_path": "02 doc/08.보고/01.회의록", "sensitivity": "L1"},
        {"entry_id": "e2", "path": str(files["quote"]), "name": files["quote"].name, "verdict": "JOIN",
         "node_path": "02 doc/01.착수", "sensitivity": "L2"},
        {"entry_id": "e3", "path": str(files["shot"]), "name": files["shot"].name, "verdict": "HOLD", "node_path": None,
         "sensitivity": "L2", "override": "doc.05.02"},
        {"entry_id": "e4", "path": str(files["dup"]), "name": files["dup"].name, "verdict": "DUP", "node_path": None, "sensitivity": "L1"},
        {"entry_id": "e5", "path": str(files["hold"]), "name": files["hold"].name, "verdict": "HOLD", "node_path": None,
         "sensitivity": "L1", "override": "HOLD"},
        {"entry_id": "e6", "path": str(files["key"]), "name": files["key"].name, "verdict": "EXCLUDE", "node_path": None,
         "sensitivity": "L3", "override": "doc.01"},
    ]
    return dict(home=home, desk=desk, cur=cur, store=store, scope=scope, conn=conn, files=files, entries=entries)


def mkplan(env, selected=("e1", "e2", "e4")):
    p = build_plan(env["entries"], set(selected), NODES, env["cur"], env["store"], env["home"], True, date(2026, 11, 17))
    return p, save_plan(env["conn"], "run1", p.to_dict())


def ctx(env, acks=("GIT_REPO_DEST", "L2_TO_REPO")):
    return GateContext(env["scope"], env["cur"], env["store"], set(), acks=set(acks))


def test_plan_contents(env):
    p, _ = mkplan(env)
    dsts = {Path(o.dst).name: o for o in p.ops}
    assert str(env["cur"] / "02 doc/08.보고") in p.mkdirs and str(env["cur"] / "02 doc/08.보고/01.회의록") in p.mkdirs
    assert str(env["cur"] / "02 doc/01.착수") not in p.mkdirs  # 이미 있음
    assert "견적서.pdf" in dsts
    assert dsts["스크린샷 2026-11-10 오후 3.12.45.png"].dst.endswith("02 doc/05.구현/02.테스트데이터/스크린샷 2026-11-10 오후 3.12.45.png")
    assert dsts["세미나안내.pdf"].dst.endswith("01.보류함/2026-11-17/세미나안내.pdf")
    assert dsts["주간회의록(1).docx"].op.value == "TRASH"
    assert any(s["name"] == "server.pem" for s in p.skipped)  # L3는 사람 지정이 있어도 이동 안 함
    assert set(p.acks_needed) == {"GIT_REPO_DEST", "L2_TO_REPO"}


def test_executor_rejects_plan_from_a_different_settings_snapshot(env):
    plan, _ = mkplan(env, selected=("e1",))
    pid = save_plan(env["conn"], "run1", plan.to_dict() | {"settings_key": "승인할 때의 설정"})
    gate_context = ctx(env)
    gate_context.settings_key = "현재 설정"
    with pytest.raises(ValueError, match="새 계획"):
        execute(env["conn"], pid, gate_context)
    assert env["files"]["meeting"].exists()
    assert load_plan(env["conn"], pid)["status"] == "READY"
    assert not list_batches(env["conn"])


def test_execute_and_undo_roundtrip(env):
    _, pid = mkplan(env)
    before = {p: p.read_bytes() for p in env["files"].values()}
    r = execute(env["conn"], pid, ctx(env))
    assert r.moved == 5 and not r.denied, (r.skipped, r.denied)
    assert (env["cur"] / "02 doc/08.보고/01.회의록/주간회의록.docx").read_bytes() == b"meeting"
    assert (env["cur"] / "02 doc/01.착수/견적서.pdf").read_bytes() == b"quote"
    assert (env["home"] / ".Trash/주간회의록(1).docx").exists()
    assert (env["store"] / "01.보류함/2026-11-17/세미나안내.pdf").exists()
    assert env["files"]["key"].exists()  # 민감 파일은 그대로
    # 같은 계획 두 번 실행 불가
    with pytest.raises(ValueError):
        execute(env["conn"], pid, ctx(env))

    u = undo(env["conn"], r.batch_id, env["scope"])
    assert u.restored == 5 and not u.stopped
    for p, data in before.items():
        assert p.read_bytes() == data
    assert not (env["cur"] / "02 doc/08.보고").exists()  # 이번 배치가 만든 폴더만 정리
    assert (env["cur"] / "02 doc/01.착수").is_dir()  # 원래 있던 폴더는 보존
    assert (env["store"] / "01.보류함").is_dir()
    assert list_batches(env["conn"])[0]["status"] == "UNDONE"


def test_git_ack_required(env):
    _, pid = mkplan(env)
    r = execute(env["conn"], pid, ctx(env, acks=()))
    assert r.moved == 2  # 휴지통·보류함(저장소 밖)만 이동, 저장소 안으로는 확인 없이 안 감
    assert all(d["reason"] == "ACK_REQUIRED" for d in r.denied)


def test_changed_and_recent_and_conflict_skipped(env):
    _, pid = mkplan(env)
    touch(env["files"]["meeting"], b"edited after plan", OLD + 10)  # 계획 후 수정
    os.utime(env["files"]["quote"], None)  # 방금 수정됨
    target = env["cur"] / "02 doc/08.보고/01.회의록"
    r = execute(env["conn"], pid, ctx(env))
    reasons = {s["name"]: s["reason"] for s in r.skipped}
    assert reasons["주간회의록.docx"] == "SRC_CHANGED"
    assert reasons["견적서.pdf"] in ("SRC_CHANGED", "RECENTLY_MODIFIED")
    assert target.is_dir()  # 폴더는 만들어졌지만 파일은 그대로
    assert env["files"]["meeting"].read_bytes() == b"edited after plan"


def test_tampered_plan_outside_scope_denied(env):
    _, pid = mkplan(env)
    body = load_plan(env["conn"], pid)["body"]
    for o in body["ops"]:
        if o["op"] == "MOVE":
            o["dst"] = str(env["home"] / "Pictures" / Path(o["src"]).name)
    env["conn"].execute("UPDATE plans SET body=? WHERE plan_id=?", (json.dumps(body), pid))
    env["conn"].commit()
    r = execute(env["conn"], pid, ctx(env))
    assert all(d["reason"] in ("OUT_OF_SCOPE", "ROLE_DENIED", "DEST_NOT_IN_SPEC") for d in r.denied)
    assert not any((env["home"] / "Pictures").iterdir())


def test_into_source_area_denied(env):
    _, pid = mkplan(env)
    body = load_plan(env["conn"], pid)["body"]
    mv = next(o for o in body["ops"] if o["op"] == "MOVE" and "회의록" in o["dst"])
    mv["dst"] = str(env["cur"] / "01 source" / "주간회의록.docx")
    body["allowed_dest_dirs"].append(str(env["cur"] / "01 source"))
    env["conn"].execute("UPDATE plans SET body=? WHERE plan_id=?", (json.dumps(body), pid))
    env["conn"].commit()
    r = execute(env["conn"], pid, ctx(env))
    assert any(d["reason"] == "DEST_PROTECTED" for d in r.denied)
    assert not (env["cur"] / "01 source/주간회의록.docx").exists()


def test_undo_stops_on_changed_file(env):
    _, pid = mkplan(env, selected=("e1",))
    r = execute(env["conn"], pid, ctx(env))
    moved = env["cur"] / "02 doc/08.보고/01.회의록/주간회의록.docx"
    touch(moved, b"user edited in new place", OLD + 99)
    u = undo(env["conn"], r.batch_id, env["scope"])
    assert u.restored == 2  # 사람 지정 2건(스크린샷·보류)은 되돌림
    assert u.stopped[0]["name"] == "주간회의록.docx" and u.stopped[0]["reason"] == "CHANGED_AFTER_BATCH"
    assert moved.read_bytes() == b"user edited in new place"
    assert (env["cur"] / "02 doc/08.보고/01.회의록").is_dir()  # 비어 있지 않으니 폴더도 유지


def test_empty_batch_cannot_be_undone(env):
    p = build_plan([], set(), NODES, env["cur"], env["store"], env["home"], True)
    pid = save_plan(env["conn"], "run1", p.to_dict())
    r = execute(env["conn"], pid, ctx(env))
    assert list_batches(env["conn"])[0]["status"] == "EMPTY"
    with pytest.raises(ValueError):
        undo(env["conn"], r.batch_id, env["scope"])


def test_write_new_never_overwrites_and_undo_trashes(env):
    from tongdory2.plan.executor import write_new

    target = env["cur"] / "README_문서색인.md"
    target.write_text("사용자가 만든 기존 파일", encoding="utf-8")
    bid, path = write_new(env["conn"], target, "# 색인", env["scope"], env["home"], "문서 색인")
    assert path.name == "README_문서색인_1.md" and target.read_text(encoding="utf-8") == "사용자가 만든 기존 파일"
    with pytest.raises(PermissionError):
        write_new(env["conn"], env["desk"] / "x.md", "x", env["scope"], env["home"], "x")  # ③에는 쓰기 불가
    u = undo(env["conn"], bid, env["scope"])
    assert u.trashed == 1 and not path.exists() and (env["home"] / ".Trash" / path.name).exists()


def test_kc_copy_keeps_originals_and_undo(env, tmp_path):
    from tongdory2.handover.knowledge_center import plan_copy
    from tongdory2.plan.executor import copy_batch

    touch(env["cur"] / "02 doc/01.착수/계약서.pdf", b"contract")
    touch(env["cur"] / "01 source/app.py", b"code")
    touch(env["cur"] / "02 doc/01.착수/id_rsa_old", b"-----BEGIN OPENSSH PRIVATE KEY-----")
    kp = plan_copy(env["cur"], env["store"], [], 1, date(2026, 11, 17), scope=env["scope"])
    names = {Path(d).name for _, d in kp.pairs}
    assert "계약서.pdf" in names and "app.py" not in names and "id_rsa_old" not in names
    bid, st = copy_batch(env["conn"], kp.pairs, kp.mkdirs, env["scope"], "지식센터 보관",
                         {str(Path(kp.dest_root) / "kc_meta.json"): "{}"})
    assert st["copied"] == len(kp.pairs)
    assert (env["cur"] / "02 doc/01.착수/계약서.pdf").read_bytes() == b"contract"  # 원본 유지
    copied = Path(kp.dest_root) / "과제/02 doc/01.착수/계약서.pdf"
    assert copied.read_bytes() == b"contract"
    # 같은 날 다시 보관하면 새 폴더(덮어쓰기 없음)
    assert plan_copy(env["cur"], env["store"], [], 1, date(2026, 11, 17), scope=env["scope"]).dest_root.endswith("2026-11-17_1")
    u = undo(env["conn"], bid, env["scope"])
    assert not copied.exists() and not Path(kp.dest_root).exists()
    assert (env["cur"] / "02 doc/01.착수/계약서.pdf").exists()
    assert u.trashed >= 2


def test_kc_copy_validates_open_source_before_creating_destination(env):
    from tongdory2.handover.knowledge_center import archive_sensitive, plan_copy
    from tongdory2.plan.executor import batch_ops, copy_batch

    changed = touch(env["cur"] / "02 doc/01.착수/보호문서.txt", b"sample before copy")
    kp = plan_copy(env["cur"], env["store"], [], 1, scope=env["scope"])
    dest = next(Path(dst) for src, dst in kp.pairs if Path(src) == changed)
    changed.write_bytes(b"-----BEGIN PRIVATE KEY-----\nDEMO-ONLY-NOT-A-KEY")
    bid, stats = copy_batch(env["conn"], kp.pairs, kp.mkdirs, env["scope"], "지식센터 보관",
                            source_allowed=lambda p, head: not archive_sensitive(p, set(), head))
    denied = next(op for op in batch_ops(env["conn"], bid) if op["dst"] == str(dest))
    assert stats["skipped"] == 1 and denied["reason"] == "SENSITIVE"
    assert not dest.exists() and changed.exists()


def test_name_conflict_skipped_not_renamed(env):
    touch(env["cur"] / "02 doc/01.착수/견적서.pdf", b"existing quote")
    p, _ = mkplan(env)
    assert not any(Path(o.dst).name.startswith("견적서_") for o in p.ops)  # 이름을 바꾸지 않음
    assert any(s["name"] == "견적서.pdf" and "같은 이름" in s["reason"] for s in p.skipped)


def test_l2_repo_ack_only_for_contracts(env):
    p = build_plan(env["entries"], {"e1"}, NODES, env["cur"], env["store"], env["home"], True, date(2026, 11, 17))
    assert "L2_TO_REPO" not in p.acks_needed  # 스크린샷(L2 이미지)만으로는 계약서 경고 없음


def test_screenshot_to_repo_needs_only_git_ack(env):
    p = build_plan(env["entries"], {"e1"}, NODES, env["cur"], env["store"], env["home"], True, date(2026, 11, 17))
    pid = save_plan(env["conn"], "run1", p.to_dict())
    r = execute(env["conn"], pid, ctx(env, acks=("GIT_REPO_DEST",)))
    assert not r.denied, r.denied  # L2 이미지(스크린샷)는 계약서 확인 없이 이동


@pytest.mark.parametrize("operation", ["WRITE_NEW", "COPY"])
def test_undo_creates_missing_trash(env, operation):
    from tongdory2.plan.executor import copy_batch, write_new

    (env["home"] / ".Trash").rmdir()
    path = env["cur"] / "draft.md"
    if operation == "WRITE_NEW":
        bid, path = write_new(env["conn"], path, "draft", env["scope"], env["home"], "draft")
    else:
        path = env["store"] / "copy.md"
        bid, _ = copy_batch(env["conn"], [(str(env["files"]["meeting"]), str(path))], [], env["scope"], "copy")
    content = path.read_bytes()
    result = undo(env["conn"], bid, env["scope"])
    assert not result.stopped and result.trashed == 1
    assert not path.exists()
    assert (env["home"] / ".Trash" / path.name).read_bytes() == content


@pytest.mark.parametrize("operation", ["MOVE", "TRASH"])
@pytest.mark.parametrize("rollback_fails", [False, True])
def test_undo_retries_after_unlink_failure(env, monkeypatch, operation, rollback_fails):
    selected = "e1" if operation == "MOVE" else "e4"
    env["entries"] = [e for e in env["entries"] if e["entry_id"] == selected]
    _, pid = mkplan(env, selected=(selected,))
    executed = execute(env["conn"], pid, ctx(env))
    row = env["conn"].execute("SELECT src, dst FROM operations WHERE batch_id=? AND op=?",
                            (executed.batch_id, operation)).fetchone()
    source, destination = map(Path, row)
    content = destination.read_bytes()
    unlink = os.unlink

    def fail(path, *args, **kwargs):
        if Path(path) == destination or (rollback_fails and Path(path) == source):
            raise PermissionError("injected")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", fail)
        result = undo(env["conn"], executed.batch_id, env["scope"])
    assert result.stopped and destination.read_bytes() == content
    assert source.exists() == rollback_fails
    result = undo(env["conn"], executed.batch_id, env["scope"])
    assert not result.stopped and result.restored == 1
    assert source.read_bytes() == content and not destination.exists()


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_draft_undo_retries_without_duplicate_trash_links(env, monkeypatch, rollback_fails):
    from tongdory2.plan.executor import write_new

    bid, path = write_new(env["conn"], env["cur"] / "draft.md", "draft", env["scope"], env["home"], "draft")
    trash = env["home"] / ".Trash" / path.name
    unlink = os.unlink

    def fail(target, *args, **kwargs):
        if Path(target) == path or (rollback_fails and Path(target) == trash):
            raise PermissionError("injected")
        return unlink(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", fail)
        result = undo(env["conn"], bid, env["scope"])
    assert result.stopped and path.read_text() == "draft"
    result = undo(env["conn"], bid, env["scope"])
    assert not result.stopped and result.trashed == 1 and not path.exists()
    assert list(trash.parent.iterdir()) == [trash]


def test_undo_preserves_different_file_at_original_path(env):
    env["entries"] = [e for e in env["entries"] if e["entry_id"] == "e1"]
    _, pid = mkplan(env, selected=("e1",))
    executed = execute(env["conn"], pid, ctx(env))
    source = env["files"]["meeting"]
    source.write_bytes(b"new unrelated file")
    result = undo(env["conn"], executed.batch_id, env["scope"])
    assert any(item["reason"] == "ORIGINAL_OCCUPIED" for item in result.stopped)
    assert source.read_bytes() == b"new unrelated file"


@pytest.mark.parametrize("stop_after_unlink", [False, True])
def test_undo_interrupted_move_recovers_verified_file(env, monkeypatch, stop_after_unlink):
    from tongdory2.plan import executor

    plan, pid = mkplan(env)
    src = env["files"]["meeting"]
    dst = next(Path(op.dst) for op in plan.ops if op.src == str(src))
    unlink = executor.os.unlink

    def interrupt(path, *args, **kwargs):
        if Path(path) == src:
            if stop_after_unlink:
                unlink(path, *args, **kwargs)
            raise KeyboardInterrupt
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(executor.os, "unlink", interrupt)
        with pytest.raises(KeyboardInterrupt):
            execute(env["conn"], pid, ctx(env))

    batch = list_batches(env["conn"])[0]
    assert batch["status"] == "RUNNING"
    assert dst.read_bytes() == b"meeting"
    result = undo(env["conn"], batch["batch_id"], env["scope"])
    assert not result.stopped
    assert src.read_bytes() == b"meeting" and not dst.exists()
    assert list_batches(env["conn"])[0]["status"] == "UNDONE"


def test_failed_archive_copy_leaves_no_partial_output_and_can_undo_folders(env, monkeypatch):
    from tongdory2.plan.executor import copy_batch

    archive = env["store"] / "archive"
    dst = archive / "meeting.docx"
    meta = archive / "kc_meta.json"

    def disk_full(inp, out):
        out.write(inp.read(2))
        raise OSError("disk full")

    monkeypatch.setattr("shutil.copyfileobj", disk_full)
    bid, stats = copy_batch(env["conn"], [(str(env["files"]["meeting"]), str(dst))],
                            [str(archive)], env["scope"], "보관", {str(meta): "{}"})
    assert stats["copied"] == 0 and stats["skipped"] == 1
    assert not list(archive.iterdir())
    assert undo(env["conn"], bid, env["scope"]).removed_dirs == 1
    assert not archive.exists()


def test_windows_archive_closes_files_before_publishing_and_preserves_existing_paths(env, monkeypatch):
    from tongdory2.plan import executor

    src = env["files"]["meeting"]
    data = b"head\r\n\x1arest of the document"
    src.write_bytes(data)
    archive = env["store"] / "archive"
    copied = archive / "사본 A.docx"
    occupied = archive / "사본 B.docx"
    meta = archive / "kc_meta.json"
    handles = []
    open_temp = executor.NamedTemporaryFile
    rename = Path.rename

    def track_open_temp(**kwargs):
        handle = open_temp(**kwargs)
        handles.append(handle)
        return handle

    def rename_without_overwrite(temporary, destination):
        assert all(handle.closed for handle in handles)
        if destination == occupied:
            destination.write_bytes(b"existing file")
            raise FileExistsError(destination)
        return rename(temporary, destination)

    with monkeypatch.context() as patch:
        patch.setattr(executor, "_WINDOWS", True)
        patch.delattr(os, "O_NONBLOCK", raising=False)
        patch.delattr(os, "fchmod", raising=False)
        patch.setattr(executor, "NamedTemporaryFile", track_open_temp)
        patch.setattr(Path, "rename", rename_without_overwrite)
        bid, stats = executor.copy_batch(env["conn"], [(str(src), str(copied)), (str(src), str(occupied))],
                                         [str(archive)], env["scope"], "보관", {str(meta): "{}"},
                                         source_allowed=lambda path, head: path == src and head == data)

    assert stats["copied"] == stats["skipped"] == 1
    assert list_batches(env["conn"])[0]["status"] == "DONE"
    assert copied.read_bytes() == src.read_bytes() == data
    assert copied.stat().st_mtime_ns == src.stat().st_mtime_ns
    assert occupied.read_bytes() == b"existing file" and meta.read_bytes() == b"{}"
    assert {p.name for p in archive.iterdir()} == {copied.name, occupied.name, meta.name}
    result = undo(env["conn"], bid, env["scope"])
    assert result.trashed == 2
    assert [row["reason"] for row in result.stopped] == ["NOT_EMPTY"]
    assert src.read_bytes() == data and list(archive.iterdir()) == [occupied]


def test_archive_keeps_same_relative_name_from_two_repositories(env, tmp_path):
    from tongdory2.fsx.paths import tilde
    from tongdory2.handover.knowledge_center import meta_json, plan_copy
    from tongdory2.plan.executor import copy_batch

    roots = list(env["scope"].roots)
    entries = []
    for folder in ("repo-a", "repo-b"):
        source = touch(env["home"] / folder / "docs" / "README.md", folder.encode())
        roots.append(RegisteredRoot(folder, env["home"] / folder, Role.IN_PLACE, True))
        entries.append({"path": str(source), "display": tilde(source, env["home"]), "name": source.name,
                        "kind": "text", "verdict": "HOLD", "git_status": "UNTRACKED"})
    scope = Scope(roots, env["home"], tmp_path / "appdata")
    plan = plan_copy(env["cur"], env["store"], entries, 1, scope=scope)
    meta = Path(plan.dest_root) / "kc_meta.json"
    _, stats = copy_batch(env["conn"], plan.pairs, plan.mkdirs, scope, "보관",
                          lambda copied: {str(meta): meta_json(plan, copied)})
    assert stats["copied"] == 2
    assert {Path(path).read_bytes() for path in plan.recovered_destinations} == {b"repo-a", b"repo-b"}
    assert json.loads(meta.read_text())["recovered_not_in_git"] == 2

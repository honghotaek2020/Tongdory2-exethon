"""A-01~A-05 제자리 목록화 — 로컬 부분(스캔·유형·Git 상태·백업), 읽기 전용 보장."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from tongdory2.catalog.inplace import _doc_type, scan_inplace
from tongdory2.fsx import gitstatus
from tongdory2.fsx.paths import real
from tongdory2.fsx.roots import RegisteredRoot, Role
from tongdory2.fsx.scope import Scope


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def test_doc_type_rules():
    assert _doc_type("docs_backup/x/설계.md", "설계", {}) == "BACKUP"
    assert _doc_type("docs/a.md", "x", {"type": "plan"}) == "PLAN"
    assert _doc_type("TASK_대사.md", "작업 지시: 스크립트", {}) == "AI_INSTRUCTION"
    assert _doc_type("docs/meeting/주간회의.md", "주간 회의", {}) == "MEETING_HANDOVER"
    assert _doc_type("docs/test/통합테스트_시나리오.md", "통합 테스트 시나리오", {}) == "TEST"
    assert _doc_type("docs/README.md", "문서 안내", {}) == "OTHER"


def test_windows_git_runs_without_console_and_uses_slash_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\tester")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(gitstatus.shutil, "which", lambda _: "git.exe")

    def run(args, *, env, creationflags, **kwargs):
        assert creationflags == subprocess.CREATE_NO_WINDOW and not kwargs["shell"]
        assert env["SystemRoot"] == r"C:\Windows" and env["USERPROFILE"] == r"C:\Users\tester"
        assert env["GIT_OPTIONAL_LOCKS"] == "0" and env["GIT_TERMINAL_PROMPT"] == "0"
        return subprocess.CompletedProcess(args, 0, "docs/한글 문서.md\0".encode() if "--cached" in args else b"")

    monkeypatch.setattr(subprocess, "run", run)
    table = gitstatus.read_git_status(tmp_path)
    assert gitstatus.status_of("docs/한글 문서.md", table) == "TRACKED"
    assert gitstatus.status_of("docs/임시/하위/한글.md", {"docs/임시": "IGNORED"}) == "IGNORED"


def test_scan_inplace_readonly_and_git(tmp_path):
    home = real(tmp_path / "home")
    repo = home / "dev/repo"
    (repo / "docs/design").mkdir(parents=True)
    (repo / "docs_backup/design").mkdir(parents=True)
    (repo / ".claude/skills").mkdir(parents=True)
    (repo / "docs/design/API설계.md").write_text("# API 설계\n## 엔드포인트\n", encoding="utf-8")
    (repo / "docs_backup/design/API설계.md").write_text("# API 설계\n## 엔드포인트\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("# 규칙", encoding="utf-8")
    (repo / ".claude/skills/s.md").write_text("# skill", encoding="utf-8")
    (repo / ".gitignore").write_text("docs/\ndocs_backup/\n.claude/\n", encoding="utf-8")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(repo)]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "-f", ".gitignore", "CLAUDE.md"], check=True)  # 전역 gitignore와 무관하게
    subprocess.run(git + ["commit", "-qm", "init"], check=True)
    before_git = _tree_hash(repo / ".git")
    before = _tree_hash(repo)
    root = RegisteredRoot("r4", repo, Role.IN_PLACE, True)
    ents, summ = scan_inplace(root, Scope([root], home, tmp_path / "ad"), home)
    by = {Path(e.path).relative_to(repo).as_posix(): e for e in ents}
    assert by["CLAUDE.md"].reason_code == "AI_CONTEXT" and by["CLAUDE.md"].git_status == "TRACKED"
    assert by[".claude/skills/s.md"].reason_code == "AI_CONTEXT"
    assert by["docs/design/API설계.md"].git_status == "IGNORED" and by["docs/design/API설계.md"].doc_type == "DESIGN"
    assert by["docs_backup/design/API설계.md"].verdict == "DUP"
    assert summ["md_total"] == 4 and summ["not_in_git"] == 3
    assert _tree_hash(repo) == before and _tree_hash(repo / ".git") == before_git  # 읽기 전용

"""TC T-01~T-03: 대상 등록, 등록 범위 밖 접근 금지, 위험 경로 차단."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from tongdory2.fsx.paths import is_link_stat, is_within, real, tilde
from tongdory2.fsx.roots import (
    Code,
    Decision,
    RegisteredRoot,
    Role,
    _windows_path_denial,
    can_save,
    check_root,
    discover_candidates,
)
from tongdory2.fsx.scope import Access, Scope, ScopeReason


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    for d in (
        "Desktop", "Downloads", "Pictures", "Library/Preferences", "Library/CloudStorage/GoogleDrive-x",
        ".ssh", "Documents/카카오톡 받은 파일", "Documents/ws한빛카드_카드발급_2023/02 doc",
        f"Documents/ws한빛카드_QR결제_{datetime.now().year}/02 doc", "dev/qrpay-settle-batch/docs",
    ):
        (h / d).mkdir(parents=True)
    (h / "dev/qrpay-settle-batch/.git").mkdir()
    for i in range(12):
        (h / f"dev/qrpay-settle-batch/docs/note{i}.md").write_text("# n", encoding="utf-8")
    (h / "Desktop/회의록.docx").write_bytes(b"x")
    return real(h)


@pytest.fixture()
def app_data(tmp_path: Path) -> Path:
    p = tmp_path / "appdata"
    p.mkdir()
    return real(p)


def reg(path: Path, role: Role, confirmed: bool = True, i: int = 0) -> RegisteredRoot:
    return RegisteredRoot(root_id=f"root_{i}", path=real(path), role=role, confirmed=confirmed)


# ---------------- T-03 위험 경로 ----------------

def test_windows_scope_uses_drive_and_component_boundaries():
    root = PureWindowsPath("C:/자료/Downloads")
    child = PureWindowsPath("c:/자료/downloads/하위/회의록.txt")
    assert is_within(child, root)
    assert tilde(child, root) == "~/하위/회의록.txt"
    assert not is_within(PureWindowsPath("C:/자료/DownloadsBackup/파일.txt"), root)
    assert not is_within(PureWindowsPath("D:/자료/Downloads/파일.txt"), root)
    assert not is_within(PureWindowsPath("//host/backup/자료/파일.txt"), PureWindowsPath("//host/docs/자료"))


def test_windows_protects_system_and_profile_directories(monkeypatch):
    home = PureWindowsPath("C:/Users/Tester")
    monkeypatch.setenv("SystemRoot", "C:/Windows")
    monkeypatch.setenv("ProgramFiles", "D:/설치")
    monkeypatch.setenv("LOCALAPPDATA", "D:/개인설정")
    assert _windows_path_denial(PureWindowsPath("D:/"), [home]) == Code.ROOT_SYSTEM
    assert _windows_path_denial(PureWindowsPath("C:/WINDOWS/System32"), [home]) == Code.ROOT_SYSTEM
    assert _windows_path_denial(PureWindowsPath("D:/설치/도구"), [home]) == Code.ROOT_SYSTEM
    assert _windows_path_denial(PureWindowsPath("D:/$Recycle.Bin/sid"), [home]) == Code.ROOT_SYSTEM
    assert _windows_path_denial(PureWindowsPath("D:/개인설정/앱"), [home]) == Code.ROOT_LIBRARY
    assert _windows_path_denial(home / "AppData/Local/Temp", [home]) == Code.ROOT_LIBRARY
    assert _windows_path_denial(home / "Downloads/하위", [home]) is None


def test_junction_is_treated_as_link():
    import stat

    junction = SimpleNamespace(st_mode=stat.S_IFDIR, st_reparse_tag=0xA0000003)
    assert is_link_stat(junction)
    assert not is_link_stat(SimpleNamespace(st_mode=stat.S_IFDIR))


@pytest.mark.parametrize("path", ["/", "/System", "/usr/bin", "/Users", "/Applications"])
def test_system_paths_refused(path, home, app_data):
    c = check_root(path, Role.COLLECT, [], home, app_data)
    assert c.decision == Decision.REFUSE


def test_library_and_secret_dirs_refused(home, app_data):
    for rel in ("Library", "Library/Preferences", ".ssh"):
        c = check_root(home / rel, Role.COLLECT, [], home, app_data)
        assert c.decision == Decision.REFUSE, rel


def test_cloud_sync_under_library_is_warning_not_refusal(home, app_data):
    c = check_root(home / "Library/CloudStorage/GoogleDrive-x", Role.COLLECT, [], home, app_data)
    assert c.decision == Decision.WARN and Code.ROOT_SYNC in c.codes
    assert not can_save(c, [])
    assert can_save(c, [Code.ROOT_SYNC])


def test_home_wide_only_as_collect_with_ack(home, app_data):
    assert check_root(home, Role.CURRENT, [], home, app_data).decision == Decision.REFUSE
    c = check_root(home, Role.COLLECT, [], home, app_data)
    assert c.decision == Decision.WARN and Code.ROOT_HOME_WIDE in c.codes


def test_app_data_refused(home, app_data):
    assert check_root(app_data, Role.STORAGE, [], home, app_data).decision == Decision.REFUSE


def test_storage_may_not_exist_yet(home, app_data):
    c = check_root(home / "Documents/자료보관센터", Role.STORAGE, [], home, app_data)
    assert c.decision == Decision.OK


# ---------------- T-01 겹침 규칙 ----------------

def test_same_path_two_roles_refused(home, app_data):
    existing = [reg(home / "Desktop", Role.COLLECT)]
    c = check_root(home / "Desktop", Role.STORAGE, existing, home, app_data)
    assert c.decision == Decision.REFUSE and Code.ROOT_OVERLAP_CONFLICT in c.codes


def test_in_place_inside_current_allowed_but_baseline_inside_current_refused(home, app_data):
    cur = home / f"Documents/ws한빛카드_QR결제_{datetime.now().year}"
    existing = [reg(cur, Role.CURRENT)]
    assert check_root(cur / "02 doc", Role.IN_PLACE, existing, home, app_data).decision == Decision.OK
    assert check_root(cur / "02 doc", Role.BASELINE, existing, home, app_data).decision == Decision.REFUSE


def test_collect_inside_in_place_refused(home, app_data):
    existing = [reg(home / "dev/qrpay-settle-batch", Role.IN_PLACE)]
    c = check_root(home / "dev/qrpay-settle-batch/docs", Role.COLLECT, existing, home, app_data)
    assert c.decision == Decision.REFUSE


def test_discover_candidates(home):
    cands = {c.path_display: c for c in discover_candidates(home)}
    year = datetime.now().year
    assert cands["~/Documents/ws한빛카드_카드발급_2023"].suggested_role == Role.BASELINE
    assert cands[f"~/Documents/ws한빛카드_QR결제_{year}"].suggested_role == Role.CURRENT
    assert cands["~/Desktop"].suggested_role == Role.COLLECT
    assert cands["~/Documents/카카오톡 받은 파일"].suggested_role == Role.COLLECT
    assert cands["~/dev/qrpay-settle-batch"].suggested_role == Role.IN_PLACE
    assert cands["~/dev/qrpay-settle-batch"].md_count == 12
    assert cands["~/Documents/자료보관센터"].suggested_role == Role.STORAGE
    assert "~/Pictures" not in cands


# ---------------- T-02 등록 범위·권한 ----------------

@pytest.fixture()
def scope(home, app_data) -> Scope:
    year = datetime.now().year
    roots = [
        reg(home / "Documents/ws한빛카드_카드발급_2023", Role.BASELINE, i=1),
        reg(home / f"Documents/ws한빛카드_QR결제_{year}", Role.CURRENT, i=2),
        reg(home / "Desktop", Role.COLLECT, i=3),
        reg(home / "dev/qrpay-settle-batch", Role.IN_PLACE, i=4),
    ]
    storage = home / "Documents/자료보관센터"
    storage.mkdir()
    roots.append(reg(storage, Role.STORAGE, i=5))
    roots.append(reg(home / "Downloads", Role.COLLECT, confirmed=False, i=6))  # 미확정
    return Scope(roots, home, app_data)


def test_unregistered_path_out_of_scope(scope, home):
    r = scope.check(home / "Pictures/a.png", Access.READ)
    assert not r.allowed and r.reason == ScopeReason.OUT_OF_SCOPE


def test_unconfirmed_root_out_of_scope(scope, home):
    assert scope.check(home / "Downloads/x.pdf", Access.READ).reason == ScopeReason.OUT_OF_SCOPE


def test_writes_only_in_current_and_storage(scope, home):
    year = datetime.now().year
    cur = home / f"Documents/ws한빛카드_QR결제_{year}"
    storage = home / "Documents/자료보관센터"
    assert scope.check(cur / "02 doc/08.보고/01.회의록/a.docx", Access.MOVE_DST).allowed
    assert scope.check(storage / "01.보류함/2026-11-17/a.pdf", Access.MOVE_DST).allowed
    assert scope.check(storage / "02.지식센터/x", Access.COPY_DST).allowed
    for p in (home / "Desktop/new", home / "Documents/ws한빛카드_카드발급_2023/x", home / "dev/qrpay-settle-batch/docs/x.md"):
        for a in (Access.MOVE_DST, Access.MKDIR, Access.WRITE_NEW, Access.COPY_DST):
            r = scope.check(p, a)
            assert not r.allowed and r.reason == ScopeReason.ROLE_DENIED, (p, a)


def test_in_place_is_read_only(scope, home):
    p = home / "dev/qrpay-settle-batch/docs/note1.md"
    assert scope.check(p, Access.READ).allowed
    assert scope.check(p, Access.COPY_SRC).allowed
    assert not scope.check(p, Access.MOVE_SRC).allowed


def test_baseline_cannot_be_move_source(scope, home):
    p = home / "Documents/ws한빛카드_카드발급_2023/02 doc"
    assert scope.check(p, Access.READ).allowed
    assert not scope.check(p, Access.MOVE_SRC).allowed


def test_in_place_nested_in_current_is_read_only(home, app_data):
    year = datetime.now().year
    cur = home / f"Documents/ws한빛카드_QR결제_{year}"
    s = Scope([reg(cur, Role.CURRENT, i=1), reg(cur / "02 doc", Role.IN_PLACE, i=2)], home, app_data)
    assert not s.check(cur / "02 doc/a.md", Access.MOVE_DST).allowed  # 가장 깊은 루트(④)가 결정
    assert s.check(cur / "README_인수인계.md", Access.WRITE_NEW).allowed


def test_symlink_component_rejected_and_target_classified(scope, home):
    link = home / "Desktop/사진링크"
    os.symlink(home / "Pictures", link)
    r = scope.check(link / "a.png", Access.READ)
    assert not r.allowed and r.reason == ScopeReason.SYMLINK_IN_PATH
    assert scope.link_target_in_scope(link) is False  # → EXCLUDE OUT_OF_SCOPE
    inner = home / "Desktop/회의록링크.docx"
    os.symlink(home / "Desktop/회의록.docx", inner)
    assert scope.link_target_in_scope(inner) is True  # → EX_LINK


def test_dotdot_escape_rejected(scope, home):
    r = scope.check(str(home / "Desktop") + "/../Pictures/a.png", Access.READ)
    assert not r.allowed and r.reason == ScopeReason.OUT_OF_SCOPE


def test_denied_path_inside_broad_collect_root(home, app_data):
    s = Scope([reg(home, Role.COLLECT, i=1)], home, app_data)
    assert s.check(home / "Desktop/a", Access.READ).allowed
    assert s.check(home / "Library/Preferences/x.plist", Access.READ).reason == ScopeReason.DENIED_PATH
    assert s.check(home / ".ssh/id_rsa", Access.READ).reason == ScopeReason.DENIED_PATH


def test_trash_destination_only_home_trash(scope, home):
    assert scope.check(home / ".Trash/WBS(1).xlsx", Access.TRASH_DST).allowed
    assert not scope.check(home / "Desktop/WBS(1).xlsx", Access.TRASH_DST).allowed


def test_collect_in_git_repo_and_large_root_warn(home, app_data, monkeypatch):
    import tongdory2.fsx.roots as R

    repo_docs = home / "dev/qrpay-settle-batch/docs"
    c = check_root(repo_docs, Role.COLLECT, [], home, app_data)
    assert c.decision == Decision.WARN and Code.ROOT_IN_GIT_REPO in c.codes
    assert check_root(repo_docs, Role.IN_PLACE, [], home, app_data).decision == Decision.OK
    monkeypatch.setattr(R, "LARGE_ROOT_FILES", 5)
    for i in range(7):
        (home / f"Downloads/f{i}.txt").write_text("x")
    c = check_root(home / "Downloads", Role.COLLECT, [], home, app_data)
    assert Code.ROOT_TOO_LARGE in c.codes


def test_env_file_path_source_only():
    """소스 실행만 저장소 최상위 .env(실행 설정)를 읽고, 배포본은 화면 입력과 앱 데이터 폴더만 쓴다."""
    from tongdory2 import config

    assert config.env_file_path(compiled=False) == config.PROJECT_ROOT / ".env"
    assert config.env_file_path(compiled=True) is None


def test_app_answers_on_tongdory_localhost_and_rejects_foreign_hosts(tmp_path):
    """브라우저 주소 tongdory.localhost를 받고, 이 PC가 아닌 이름은 거부한다."""
    from fastapi.testclient import TestClient

    from tongdory2.config import Settings
    from tongdory2.web.app import create_app

    s = Settings(_env_file=None, LINER_API_KEY="", TONGDORY_HOME=str(tmp_path), TONGDORY_DATA_DIR=str(tmp_path / "d"))
    assert s.app_url == "http://tongdory.localhost:8765/" and s.fallback_url == "http://127.0.0.1:8765/"
    app = create_app(s)
    for base in ("http://tongdory.localhost:8765", "http://127.0.0.1:8765", "http://localhost:8765"):
        assert TestClient(app, base_url=base).get("/").status_code == 200
    assert TestClient(app, base_url="http://evil.example:8765").get("/").status_code == 400
    # 외부 도메인으로 바꾸려 해도 기본 이름으로 돌아간다.
    bad = Settings(_env_file=None, TONGDORY_BROWSER_HOST="evil.example", TONGDORY_DATA_DIR=str(tmp_path / "d2"))
    assert bad.app_host == "tongdory.localhost" and "evil.example" not in bad.allowed_hosts

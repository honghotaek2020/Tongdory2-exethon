"""A-03 Git 추적 상태 — 읽기 전용 git 하위 명령만 실행 (03 §3.4).

허용: rev-parse --show-toplevel, ls-files --cached / --others --exclude-standard / --others --ignored --exclude-standard
금지: status(인덱스 갱신 가능), add·commit·checkout·config 등 상태를 바꾸는 모든 명령, .gitignore 쓰기
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unicodedata
from pathlib import Path, PurePosixPath

ALLOWED = {
    ("rev-parse", "--show-toplevel"),
    ("ls-files", "-z", "--cached"),
    ("ls-files", "-z", "--others", "--exclude-standard"),
    ("ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory", "--no-empty-directory"),
    ("ls-files", "-z", "--others", "--ignored", "--exclude-standard"),
}
TIMEOUT = 10
MAX_OUT = 20 * 1024 * 1024


def _git(repo: Path, *args: str) -> str | None:
    if tuple(args) not in ALLOWED:
        raise ValueError(f"허용되지 않은 git 명령: {args}")
    exe = shutil.which("git")
    if not exe:
        return None
    env = {"PATH": os.environ.get("PATH", ""), "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags:
        env_keys = ("SystemRoot", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME")
        env.update({key: os.environ[key] for key in env_keys if key in os.environ})
    else:
        env["HOME"] = os.environ.get("HOME", "")
    try:
        cp = subprocess.run([exe, "-C", str(repo), "-c", "core.fsmonitor=false", "-c", "core.quotepath=false", *args],
                            shell=False, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT, env=env,
                            creationflags=creationflags)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if cp.returncode != 0 or len(cp.stdout) > MAX_OUT:
        return None
    return cp.stdout.decode("utf-8", "replace")


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def read_git_status(root: Path) -> dict[str, str] | None:
    """root 하위 상대경로 → TRACKED | UNTRACKED | IGNORED. 저장소가 아니면 None (NO_REPO)."""
    if _git(root, "rev-parse", "--show-toplevel") is None:
        return None
    out: dict[str, str] = {}
    for status, args in (
        ("TRACKED", ("ls-files", "-z", "--cached")),
        ("UNTRACKED", ("ls-files", "-z", "--others", "--exclude-standard")),
        ("IGNORED", ("ls-files", "-z", "--others", "--ignored", "--exclude-standard")),
    ):
        txt = _git(root, *args)
        if txt is None:
            continue
        for rel in txt.split("\0"):
            if rel:
                out[_nfc(rel.rstrip("/"))] = status
    return out


def status_of(rel: str, table: dict[str, str] | None) -> str:
    """Git이 출력하는 슬래시 구분 상대 경로로 상태를 찾는다."""
    if table is None:
        return "NO_REPO"
    rel = _nfc(rel)
    if rel in table:
        return table[rel]
    # 무시된 디렉터리 아래 파일
    for parent in PurePosixPath(rel).parents:
        if table.get(parent.as_posix()) == "IGNORED":
            return "IGNORED"
    return "UNKNOWN"

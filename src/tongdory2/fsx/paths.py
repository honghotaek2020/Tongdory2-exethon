"""경로 유틸: NFC 정규화, realpath, 포함 관계, 링크 구성요소 검사.

macOS는 한글 파일명을 NFD로 돌려줄 수 있으므로 비교는 항상 NFC로 한다.
"""

from __future__ import annotations

import os
import stat
import unicodedata
from pathlib import Path, PurePath, PureWindowsPath


_JUNCTION_TAG = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def real(p: str | os.PathLike[str]) -> Path:
    """링크를 모두 해소한 절대경로(NFC). 존재하지 않아도 동작한다."""
    return Path(nfc(os.path.realpath(os.path.expanduser(os.fspath(p)))))


def absnorm(p: str | os.PathLike[str]) -> Path:
    """링크를 해소하지 않은 정규 절대경로(NFC). `..`는 정리한다."""
    return Path(nfc(os.path.normpath(os.path.abspath(os.path.expanduser(os.fspath(p))))))


def is_within(child: PurePath, parent: PurePath) -> bool:
    """child가 parent와 같거나 그 하위인가 (둘 다 정규화된 절대경로 가정).

    Windows에서는 드라이브·공유 경로와 대소문자를 Windows 규칙으로 비교한다.
    POSIX에서는 대량 탐색에 쓰는 기존의 대소문자 구분 문자열 비교를 유지한다.
    """
    if isinstance(child, PureWindowsPath):
        return child.is_relative_to(parent)
    c, p = str(child), str(parent)
    if c == p:
        return True
    base = p if p.endswith("/") else p + "/"
    return c.startswith(base)


def is_link_stat(info: os.stat_result) -> bool:
    """탐색에서 따라가지 않는 심볼릭 링크와 Windows 정션을 판별한다."""
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) == _JUNCTION_TAG


def first_symlink_component(path: Path, start: Path) -> Path | None:
    """start 아래에서 처음 만나는 심볼릭 링크 또는 Windows 정션을 돌려준다.

    path는 start 하위의 absnorm 경로여야 한다. 존재하지 않는 구성요소에서 멈춘다.
    """
    try:
        rel = path.relative_to(start)
    except ValueError:
        return None
    cur = start
    for part in rel.parts:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            return None
        if is_link_stat(st):
            return cur
    return None


def tilde(path: PurePath, home: PurePath) -> str:
    """화면 표시용 `~` 축약."""
    if is_within(path, home):
        rel = path.relative_to(home)
        return "~" if not rel.parts else f"~/{rel.as_posix()}"
    return str(path)

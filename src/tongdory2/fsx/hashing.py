"""해시·중복·버전 묶음 (02_정책 §6, R-2.4).

- DUP = sha256과 크기가 모두 같을 때만(R-6.5). 이름 유사만으로는 중복 아님.
- 버전 묶음 = 정규화 줄기 + 같은 확장자 계열(R-6.1·R-6.2). 최신 = mtime 최대(R-6.3).
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from tongdory2.fsx.scan import ScanItem
from tongdory2.fsx.screenshots import is_screenshot

if TYPE_CHECKING:
    from tongdory2.fsx.scope import Scope

_CHUNK = 1 << 20


def sha256_file(path: Path, scope: "Scope | None" = None) -> str:
    """링크를 따라가지 않고(O_NOFOLLOW) 일반 파일만 해시한다. scope를 주면 READ 검사를 먼저 한다.

    링크·일반 파일 아님·범위 밖이면 OSError/PermissionError.
    """
    if scope is not None:
        from tongdory2.fsx.scope import Access

        r = scope.check(path, Access.READ)
        if not r.allowed:
            raise PermissionError(f"scope: {r.reason.value}")
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise OSError(f"not a regular file: {path}")
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        while chunk := os.read(fd, _CHUNK):
            h.update(chunk)
    finally:
        os.close(fd)
    return h.hexdigest()


# ---------------- 중복 ----------------
_COPY_TOKEN = re.compile(r"\(\d+\)|복사본|(?:^|[_\s\-])copy(?=$|[_\s\-\d])|^copy ?of", re.I)


def has_copy_token(name: str) -> bool:
    stem = Path(unicodedata.normalize("NFC", name)).stem
    return bool(_COPY_TOKEN.search(stem))


def dup_groups(items: list[ScanItem], hashes: dict[Path, str],
               current_index: dict[str, Path] | None = None) -> dict[Path, Path]:
    """중복 → 남길 원본. 남길 것: 현재 과제 안 > (1)·복사본 토큰 없음 > mtime 이른 것 > 경로순(R-6.7).

    current_index: sha256 → 현재 과제(②) 안 파일 경로. 스캔 항목과 해시가 같으면 그 파일이 원본.
    스크린샷은 제외한다 — 날짜별 묶음에서 사람이 지정(결정 #40). 데모 매니페스트도 스크린샷을 DUP로 보지 않는다
    (데모 생성기는 같은 화면 종류를 바이트가 같은 PNG로 만든다).
    """
    from tongdory2.fsx.roots import Role

    by_key: dict[tuple[str, int], list[ScanItem]] = defaultdict(list)
    for it in items:
        if it.is_dir or it.is_link or it.path not in hashes or is_screenshot(it.name):
            continue
        by_key[(hashes[it.path], it.size)].append(it)

    out: dict[Path, Path] = {}
    for (h, _size), group in by_key.items():
        cur = (current_index or {}).get(h)
        if cur is not None:
            for it in group:
                if it.path != cur:
                    out[it.path] = cur
            continue
        if len(group) < 2:
            continue
        keep = min(group, key=lambda it: (it.role != Role.CURRENT, has_copy_token(it.name), it.mtime,
                                          str(it.path)))
        for it in group:
            if it is not keep:
                out[it.path] = keep.path
    return out


# ---------------- 버전 묶음 ----------------
_B = r"(?:^|[_\s\-])"  # 토큰 앞 경계 (한글도 \w라서 \b를 쓰지 않는다)
_E = r"(?=$|[_\s\-()])"
_VERSION_TOKENS = [
    re.compile(_B + r"v(?:er)?\.?\d+(?:\.\d+)*" + _E, re.I),  # _v1.2, v3, ver2
    re.compile(_B + r"(?:final|최종|수정본|수정|초안|draft)" + _E, re.I),
    re.compile(r"\s*\(\d+\)"),  # (1)
    re.compile(r"\s*-\s*복사본(?:\s*\d+)?|[_\s]복사본|" + _B + r"copy(?:\s*\d+)?" + _E, re.I),
]
_TRAILING_DATE = re.compile(r"[_\s]\d{4,8}$")
_FAMILY = {"xls": "xls", "xlsx": "xls", "xlsm": "xls", "doc": "doc", "docx": "doc", "ppt": "ppt", "pptx": "ppt",
           "hwp": "hwp", "hwpx": "hwp", "jpg": "jpg", "jpeg": "jpg", "md": "md", "markdown": "md"}


def version_stem(name: str) -> str:
    """정규화 줄기: 버전 토큰·(n)·복사본·끝 날짜 제거 후 토큰 순서 무시(`A_B` = `B_A`)."""
    s = Path(unicodedata.normalize("NFC", name)).stem
    for _ in range(3):
        before = s
        for rx in _VERSION_TOKENS:
            s = rx.sub("", s)
        if s == before:
            break
    s = _TRAILING_DATE.sub("", s.strip(" _-"))
    tokens = [t for t in re.split(r"[_\s]+", s.lower().strip(" _-")) if t]
    return "_".join(sorted(tokens))


def ext_family(name: str) -> str:
    ext = Path(name).suffix.lower().lstrip(".")
    return _FAMILY.get(ext, ext)


def version_key(name: str, *, explicit: bool = False) -> tuple[str, str] | None:
    if is_screenshot(name):
        return None
    if explicit and not any(rx.search(Path(unicodedata.normalize("NFC", name)).stem) for rx in _VERSION_TOKENS[:2]):
        return None
    stem = version_stem(name)
    return (stem, ext_family(name)) if stem else None


def version_groups(items: list[ScanItem]) -> list[list[ScanItem]]:
    """같은 줄기 + 같은 확장자 계열 2개 이상 → 한 묶음. 각 묶음은 최신(mtime 최대) 먼저.

    폴더·링크·스크린샷은 묶지 않는다(스크린샷은 날짜별 묶음이 따로 있음, 결정 #40).
    """
    groups: dict[tuple[str, str], list[ScanItem]] = defaultdict(list)
    for it in items:
        if it.is_dir or it.is_link:
            continue
        if key := version_key(it.name):
            groups[key].append(it)
    out = [sorted(g, key=lambda it: (-it.mtime, str(it.path))) for g in groups.values() if len(g) >= 2]
    out.sort(key=lambda g: str(g[0].path))
    return out

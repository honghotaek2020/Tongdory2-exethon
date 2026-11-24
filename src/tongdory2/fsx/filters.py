"""② 로컬 사전 필터 (02_정책 §3, R-16.2, §17.5). 네트워크 전송 전, 코드만.

첫 일치에서 멈춘다. 순서:
 1. 링크(R-16.2): 대상이 범위 안 → EX_LINK, 밖 → OUT_OF_SCOPE (대상 경로는 detail에 쓰지 않음)
 2. 범위 검사 실패 → OUT_OF_SCOPE
 3. 폴더 항목: .git → GIT_REPO_FOLDER, 프로젝트 마커 → PROJECT_FOLDER, AI 설정 폴더 → AI_CONTEXT,
    .app 번들 → INSTALLER, 숨김 폴더 → HIDDEN
 4. 하드링크(st_nlink>1) → EX_LINK
 5. AI_CONTEXT (경로 어느 단계든)
 6. SENSITIVE (L3) — 숨김보다 먼저(`.env.bak`은 '민감'으로 보여야 함)
 7. HIDDEN (.DS_Store·._*·~$ 잠금 파일·점 파일)
 8. TEMP_DOWNLOAD (.crdownload .part .download)
 9. OPEN_FILE (같은 폴더에 Office 잠금 파일)
10. INSTALLER
11. RECENTLY_MODIFIED (결정 #9: 2시간)
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from tongdory2.fsx.scan import AI_CONTEXT_DIRS, ScanItem, project_marker
from tongdory2.fsx.scope import Access, Scope
from tongdory2.fsx.sensitivity import KIND_LABEL, Sensitivity, classify_sensitivity, read_head

HIDDEN = "HIDDEN"
TEMP_DOWNLOAD = "TEMP_DOWNLOAD"
INSTALLER = "INSTALLER"
RECENTLY_MODIFIED = "RECENTLY_MODIFIED"
EX_LINK = "EX_LINK"
OUT_OF_SCOPE = "OUT_OF_SCOPE"
SENSITIVE = "SENSITIVE"
PROJECT_FOLDER = "PROJECT_FOLDER"
GIT_REPO_FOLDER = "GIT_REPO_FOLDER"
AI_CONTEXT = "AI_CONTEXT"
OPEN_FILE = "OPEN_FILE"

# 03 명세·데모 매니페스트의 같은 뜻 코드 (03 `SYMLINK` = 02 `EX_LINK`)
REASON_ALIASES = {"SYMLINK": EX_LINK, "SYSTEM_HIDDEN": HIDDEN}

SYSTEM_FILES = {".ds_store", "thumbs.db", "desktop.ini", "icon\r", ".localized"}
TEMP_EXTS = {".crdownload", ".part", ".download", ".partial", ".opdownload"}
INSTALLER_EXTS = {".dmg", ".pkg", ".mpkg", ".exe", ".msi", ".deb", ".rpm", ".apk", ".ipa", ".iso", ".appimage"}
AI_CONTEXT_FILES = {
    "claude.md", "claude.local.md", "agents.md", "gemini.md", ".cursorrules", ".windsurfrules", ".clinerules",
}
MD_EXTS = {".md", ".markdown", ".mdx"}
MD_FULL_SCAN = 1 << 20  # R-17.5: ④ md는 파일 전체(≤1MB) 민감 검사


@dataclass
class FilterResult:
    excluded: bool
    reason: str | None
    detail: str
    sensitivity: Sensitivity | None = None  # 통과 항목의 민감 등급(판정했으면)


def _ex(reason: str, detail: str, sens: Sensitivity | None = None) -> FilterResult:
    return FilterResult(True, reason, detail, sens)


def is_ai_context(rel: str) -> bool:
    parts = [unicodedata.normalize("NFC", p).lower() for p in Path(rel).parts]
    for i, part in enumerate(parts):
        if part in AI_CONTEXT_DIRS or part.startswith(".aider"):
            return True
        if part == ".github" and i + 1 < len(parts):
            nxt = parts[i + 1]
            if nxt in ("instructions", "prompts", "copilot-instructions.md"):
                return True
    return bool(parts) and parts[-1] in AI_CONTEXT_FILES


def _lock_sibling(p: Path) -> bool:
    name = p.name
    cands = {f"~${name}", f".~lock.{name}#"}
    if len(name) > 2:
        cands.add(f"~${name[2:]}")  # Word: 긴 이름은 앞 2자를 잘라 잠금 파일을 만든다
    return any(os.path.lexists(p.parent / c) for c in cands)


def _zip_unpacked(p: Path) -> bool:
    return p.suffix.lower() == ".zip" and os.path.isdir(p.with_suffix("")) and not os.path.islink(p.with_suffix(""))


def _sens_for(item: ScanItem, scope: Scope) -> Sensitivity:
    sens = classify_sensitivity(item.path)
    if sens.level == "L3":
        return sens  # 이름·확장자만으로 차단 — 내용은 읽지 않는다
    full_md = item.path.suffix.lower() in MD_EXTS and item.role.value == "IN_PLACE"
    if full_md and item.size > MD_FULL_SCAN:
        return Sensitivity("L2", sens.signals + ["MD_TOO_LARGE"])
    head = read_head(item.path, scope, MD_FULL_SCAN if full_md else 4096)
    return classify_sensitivity(item.path, head) if head else sens


def classify(item: ScanItem, scope: Scope, now: float, recent_hours: float = 2.0,
             sens: Sensitivity | None = None) -> FilterResult:
    p = item.path
    name = unicodedata.normalize("NFC", p.name)
    low = name.lower()

    # 1~2. 링크·범위
    if item.is_link:
        if scope.link_target_in_scope(p):
            return _ex(EX_LINK, "링크 — 원본 따라가지 않음")
        return _ex(OUT_OF_SCOPE, "링크 — 등록 범위 밖을 가리킴")
    if not scope.check(p, Access.READ).allowed:
        return _ex(OUT_OF_SCOPE, "등록 범위 밖")

    # 3. 폴더 항목 (스캔이 통째로 돌려준 것)
    if item.is_dir:
        marker = project_marker(p)
        if marker == ".git":
            return _ex(GIT_REPO_FOLDER, "Git 저장소 폴더 — 통째 제외, 내부 미탐색")
        if marker:
            return _ex(PROJECT_FOLDER, f"프로젝트 폴더({marker}) — 통째 제외, 내부 미탐색")
        if is_ai_context(item.rel):
            return _ex(AI_CONTEXT, "AI 설정")
        if low.endswith(".app"):
            return _ex(INSTALLER, "설치 파일 — 과제 산출물 아님")
        if name.startswith("."):
            return _ex(HIDDEN, "숨김 폴더")
        return FilterResult(False, None, "")

    # 4. 하드링크
    try:
        if os.lstat(p).st_nlink > 1:
            return _ex(EX_LINK, "링크(하드링크) — 원본 따라가지 않음")
    except OSError:
        return _ex(OUT_OF_SCOPE, "파일을 확인할 수 없음")

    # 5. AI 설정
    if is_ai_context(item.rel):
        return _ex(AI_CONTEXT, "AI 설정")

    # 6. 민감
    s = sens or _sens_for(item, scope)
    if s.level == "L3":
        kind = next((KIND_LABEL[x] for x in s.signals if x in KIND_LABEL), "민감 파일")
        return _ex(SENSITIVE, f"민감: {kind}", s)

    # 7. 숨김·시스템
    if low in SYSTEM_FILES or name.startswith("._") or name.startswith("~$") or name.startswith(".~lock."):
        return _ex(HIDDEN, "시스템·임시 파일", s)
    if name.startswith("."):
        return _ex(HIDDEN, "숨김 파일", s)

    # 8~9. 미완성 다운로드·열린 파일
    if p.suffix.lower() in TEMP_EXTS:
        return _ex(TEMP_DOWNLOAD, "미완성 다운로드", s)
    if _lock_sibling(p):
        return _ex(OPEN_FILE, "열려 있는 파일", s)

    # 10. 설치 파일
    if p.suffix.lower() in INSTALLER_EXTS or _zip_unpacked(p):
        return _ex(INSTALLER, "설치 파일 — 과제 산출물 아님", s)

    # 11. 최근 수정
    if item.mtime > now - recent_hours * 3600:
        return _ex(RECENTLY_MODIFIED, "최근 수정 — 작성 중일 수 있음", s)

    return FilterResult(False, None, "", s)


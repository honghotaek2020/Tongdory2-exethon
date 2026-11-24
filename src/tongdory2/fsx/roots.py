"""S0 대상 등록: 역할, 위험 경로 검사, 겹침 검사, 자동 후보 탐지 (TC T-01~T-03).

근거: 02_정책 §16, 결정로그 #22·#24·#37~#39.
- 역할 5종: ① BASELINE ② CURRENT ③ COLLECT ④ IN_PLACE ⑤ STORAGE
- 쓰기는 ②·⑤ 두 곳만 (+ OS 휴지통). 권한 판정은 scope.py.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path, PureWindowsPath

from tongdory2.fsx.paths import is_within, real, tilde


LARGE_ROOT_FILES = 10_000  # ③·② 등록 시 이보다 많으면 경고


class Role(str, Enum):
    BASELINE = "BASELINE"  # ① 기준(과거 과제) — 읽기
    CURRENT = "CURRENT"  # ② 현재 과제 — 재배치 목적지
    COLLECT = "COLLECT"  # ③ 수집 대상 — 이동 원본
    IN_PLACE = "IN_PLACE"  # ④ 제자리 목록화 — 읽기
    STORAGE = "STORAGE"  # ⑤ 저장 위치 — 보류함·지식센터·내보내기

    @property
    def label(self) -> str:
        return ROLE_LABELS[self]


ROLE_LABELS = {
    Role.BASELINE: "참고 과제(선택)",
    Role.CURRENT: "저장 폴더",
    Role.COLLECT: "정리 대상 폴더",
    Role.IN_PLACE: "AI 협업 문서(선택)",
    Role.STORAGE: "관리용 보관함",
}

# ⑤ 저장 위치 내부 구조 (결정 #38: 사용자 번호 규칙, 밑줄·제품명 없음)
STORAGE_HOLD = "01.보류함"
STORAGE_KC = "02.지식센터"
STORAGE_EXPORT = "03.내보내기"
STORAGE_DEFAULT_NAME = "자료보관센터"  # `ws` 접두 금지 — 과제 후보로 오탐


class Decision(str, Enum):
    OK = "OK"
    WARN = "WARN"
    REFUSE = "REFUSE"


class Code(str, Enum):
    # REFUSE
    ROOT_SYSTEM = "ROOT_SYSTEM"
    ROOT_LIBRARY = "ROOT_LIBRARY"
    ROOT_HIDDEN = "ROOT_HIDDEN"
    ROOT_APP_DATA = "ROOT_APP_DATA"
    ROOT_NOT_DIR = "ROOT_NOT_DIR"
    ROOT_NO_PERMISSION = "ROOT_NO_PERMISSION"
    ROOT_OVERLAP_CONFLICT = "ROOT_OVERLAP_CONFLICT"
    ROOT_ROLE_NOT_ALLOWED = "ROOT_ROLE_NOT_ALLOWED"
    # WARN
    ROOT_HOME_WIDE = "ROOT_HOME_WIDE"
    ROOT_SYNC = "ROOT_SYNC"
    ROOT_EXTERNAL_VOLUME = "ROOT_EXTERNAL_VOLUME"
    ROOT_TOO_LARGE = "ROOT_TOO_LARGE"
    ROOT_IN_GIT_REPO = "ROOT_IN_GIT_REPO"


REFUSE_CODES = {
    Code.ROOT_SYSTEM,
    Code.ROOT_LIBRARY,
    Code.ROOT_HIDDEN,
    Code.ROOT_APP_DATA,
    Code.ROOT_NOT_DIR,
    Code.ROOT_NO_PERMISSION,
    Code.ROOT_OVERLAP_CONFLICT,
    Code.ROOT_ROLE_NOT_ALLOWED,
}

MESSAGES = {
    Code.ROOT_SYSTEM: "시스템 폴더는 등록할 수 없습니다",
    Code.ROOT_LIBRARY: "앱 설정·보안 폴더는 등록할 수 없습니다",
    Code.ROOT_HIDDEN: "숨김 폴더는 등록할 수 없습니다",
    Code.ROOT_APP_DATA: "이 앱의 데이터 폴더는 등록할 수 없습니다",
    Code.ROOT_NOT_DIR: "폴더가 아니거나 존재하지 않습니다",
    Code.ROOT_NO_PERMISSION: "읽기 권한이 없습니다",
    Code.ROOT_OVERLAP_CONFLICT: "이미 등록한 폴더와 역할이 겹칩니다",
    Code.ROOT_ROLE_NOT_ALLOWED: "이 폴더에는 해당 역할을 줄 수 없습니다",
    Code.ROOT_HOME_WIDE: "홈 전체는 범위가 넓습니다 — Documents·Desktop처럼 하위 폴더를 지정하세요",
    Code.ROOT_SYNC: "이동하면 다른 기기에도 동기화됩니다",
    Code.ROOT_EXTERNAL_VOLUME: "다른 볼륨 — 목록화만 가능할 수 있습니다",
    Code.ROOT_TOO_LARGE: f"파일이 {LARGE_ROOT_FILES:,}개가 넘어 분류에 시간이 걸립니다.",
    Code.ROOT_IN_GIT_REPO: "Git 저장소 안의 폴더입니다. 파일이 이동되면 Git에서는 삭제된 것으로 보입니다.",
}

def count_files_capped(root: Path, cap: int = LARGE_ROOT_FILES) -> int:
    """이름만 센다(본문 0바이트), cap+1에서 멈춤. .git·빌드 폴더 제외, 링크 미추적."""
    n = 0
    for cur, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS_COUNT]
        if os.name == "nt":
            dirs[:] = [d for d in dirs if not (Path(cur) / d).is_junction()]
        n += len(files)
        if n > cap:
            return n
    return n


def in_git_repo(p: Path, home: Path | None = None) -> bool:
    """p 또는 그 상위에 .git이 있는가 — 홈 폴더 아래까지만 본다(홈 자체가 dotfiles 저장소인 경우 오탐 방지)."""
    stop = {real(h) for h in ([home] if home else [])} | {real(Path.home())}
    cur = p
    while True:
        if cur in stop:
            return False
        if (cur / ".git").exists():
            return True
        if cur.parent == cur:
            return False
        cur = cur.parent


_SKIP_DIRS_COUNT = {".git", "node_modules", "build", "dist", "target", ".venv", "venv", "__pycache__"}

SYSTEM_ROOTS = [
    "/System", "/Library", "/Applications", "/usr", "/bin", "/sbin", "/etc",
    "/var", "/private", "/opt", "/cores",
]
SYSTEM_EXACT = ["/", "/Volumes", "/Users"]
WINDOWS_SYSTEM_VARS = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData")
WINDOWS_USER_VARS = ("LOCALAPPDATA", "APPDATA")
HOME_SECRET_DIRS = [".Trash", ".ssh", ".gnupg", ".aws", ".config"]
SYNC_UNDER_LIBRARY = ["Library/CloudStorage", "Library/Mobile Documents"]
SYNC_NAME_MARKERS = ("Dropbox", "Google Drive", "OneDrive")


@dataclass
class RootCheck:
    decision: Decision
    realpath: Path
    codes: list[Code] = field(default_factory=list)

    @property
    def messages(self) -> list[str]:
        return [MESSAGES[c] for c in self.codes]

    @property
    def warn_codes(self) -> list[Code]:
        return [c for c in self.codes if c not in REFUSE_CODES]


@dataclass
class RegisteredRoot:
    root_id: str
    path: Path  # realpath, NFC
    role: Role
    confirmed: bool = False
    acks: list[Code] = field(default_factory=list)
    linked_root_id: str | None = None  # ④ → 연결 ② 과제


def _homes(home: Path) -> list[Path]:
    hs = {real(home), real(Path.home())}
    return sorted(hs)


def _windows_path_denial(p: PureWindowsPath, homes: list[PureWindowsPath]) -> Code | None:
    """드라이브 루트와 Windows의 시스템·사용자 설정 폴더를 보호한다."""
    if p.anchor and p == PureWindowsPath(p.anchor):
        return Code.ROOT_SYSTEM
    if any(is_within(p, PureWindowsPath(p.anchor) / name) for name in ("$Recycle.Bin", "System Volume Information")):
        return Code.ROOT_SYSTEM
    # 환경 변수로 이동된 Windows 폴더와 기본 위치를 함께 보호한다.
    system_root = PureWindowsPath(os.environ.get("SystemRoot") or homes[0].anchor)
    drive = PureWindowsPath(system_root.anchor)
    for name in ("Windows", "Program Files", "Program Files (x86)", "ProgramData"):
        if is_within(p, drive / name):
            return Code.ROOT_SYSTEM
    for key in WINDOWS_SYSTEM_VARS:
        if (value := os.environ.get(key)) and is_within(p, PureWindowsPath(value)):
            return Code.ROOT_SYSTEM
    for key in WINDOWS_USER_VARS:
        if (value := os.environ.get(key)) and is_within(p, PureWindowsPath(value)):
            return Code.ROOT_LIBRARY
    if any(is_within(p, h / "AppData") for h in homes):
        return Code.ROOT_LIBRARY
    return None


def is_denied_path(p: Path, home: Path, app_data: Path, for_registration: bool = False) -> Code | None:
    """등록 루트 안에 있어도 항상 범위 밖인 경로인지 (R-16.10). p는 realpath.

    for_registration=True면 홈 아래 숨김 폴더 자체를 루트로 등록하는 것도 거부한다.
    (루트 안의 숨김 파일·폴더는 범위 밖이 아니라 필터 단계에서 판정한다.)
    """
    if isinstance(p, PureWindowsPath):
        if is_within(p, real(app_data)):
            return Code.ROOT_APP_DATA
        homes = _homes(home)
        if denied := _windows_path_denial(p, homes):
            return denied
    else:
        if str(p) in SYSTEM_EXACT:
            return Code.ROOT_SYSTEM
        for sr in SYSTEM_ROOTS:
            if is_within(p, Path(sr)):
                # 홈이 /private 아래 등에 있는 테스트 환경을 위해 홈 하위는 시스템 판정에서 제외
                if any(is_within(p, h) for h in _homes(home)):
                    break
                return Code.ROOT_SYSTEM
        if is_within(p, real(app_data)):
            return Code.ROOT_APP_DATA
        homes = _homes(home)
    for h in homes:
        lib = h / "Library"
        if is_within(p, lib):
            if any(is_within(p, h / s2) for s2 in SYNC_UNDER_LIBRARY):
                return None  # 동기화 폴더는 거부가 아니라 경고 (결정 #24)
            return Code.ROOT_LIBRARY
        for d in HOME_SECRET_DIRS:
            if is_within(p, h / d):
                return Code.ROOT_LIBRARY
        if for_registration and is_within(p, h) and p != h:
            rel = p.relative_to(h)
            if any(part.startswith(".") for part in rel.parts):
                return Code.ROOT_HIDDEN
    return None


def _is_sync(p: Path, home: Path) -> bool:
    for h in _homes(home):
        if any(is_within(p, h / s2) for s2 in SYNC_UNDER_LIBRARY):
            return True
    return any(m in p.parts for m in SYNC_NAME_MARKERS)


def check_root(
    raw_path: str | os.PathLike[str],
    role: Role,
    existing: list[RegisteredRoot],
    home: Path,
    app_data: Path,
) -> RootCheck:
    """등록 검사 (R-16.7): 존재·디렉터리·권한 → realpath → 위험 경로 → 겹침."""
    raw = os.fspath(raw_path)
    if raw == "~" or raw.startswith("~/"):  # #63 설정한 홈(TONGDORY_HOME) 기준으로 펼친다
        raw = str(home) + raw[1:]
    p = real(raw)
    codes: list[Code] = []

    if role == Role.STORAGE and not p.exists():
        # ⑤는 아직 없는 폴더도 허용: 부모가 있는 폴더면 확정 시 생성한다
        parent = p.parent
        if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
            return RootCheck(Decision.REFUSE, p, [Code.ROOT_NOT_DIR])
        denied = is_denied_path(p, home, app_data, for_registration=True)
        if denied:
            return RootCheck(Decision.REFUSE, p, [denied])
        if _overlap_conflict(p, role, existing):
            return RootCheck(Decision.REFUSE, p, [Code.ROOT_OVERLAP_CONFLICT])
        return RootCheck(Decision.OK, p, [])

    if not p.is_dir():
        return RootCheck(Decision.REFUSE, p, [Code.ROOT_NOT_DIR])
    if not os.access(p, os.R_OK | os.X_OK):
        return RootCheck(Decision.REFUSE, p, [Code.ROOT_NO_PERMISSION])

    denied = is_denied_path(p, home, app_data, for_registration=True)
    if denied:
        return RootCheck(Decision.REFUSE, p, [denied])

    if any(p == h for h in _homes(home)):
        if role != Role.COLLECT:  # R-16.11 홈 전체는 ③만
            return RootCheck(Decision.REFUSE, p, [Code.ROOT_ROLE_NOT_ALLOWED, Code.ROOT_HOME_WIDE])
        codes.append(Code.ROOT_HOME_WIDE)

    if _is_sync(p, home):
        codes.append(Code.ROOT_SYNC)

    try:
        if os.stat(p).st_dev != os.stat(real(home)).st_dev:
            codes.append(Code.ROOT_EXTERNAL_VOLUME)
    except OSError:
        pass

    conflict = _overlap_conflict(p, role, existing)
    if conflict:
        return RootCheck(Decision.REFUSE, p, [Code.ROOT_OVERLAP_CONFLICT])

    if role in (Role.COLLECT, Role.CURRENT) and count_files_capped(p) > LARGE_ROOT_FILES:
        codes.append(Code.ROOT_TOO_LARGE)
    if role == Role.COLLECT and in_git_repo(p, home):
        codes.append(Code.ROOT_IN_GIT_REPO)

    return RootCheck(Decision.WARN if codes else Decision.OK, p, codes)


# 허용되는 중첩 (inner_role, outer_role). 가장 깊은 등록이 하위 역할을 정한다 (R-16.4).
_ALLOWED_NESTING = {
    (Role.IN_PLACE, Role.CURRENT),  # 현재 과제 저장소의 docs/
    (Role.BASELINE, Role.COLLECT),
    (Role.CURRENT, Role.COLLECT),
    (Role.IN_PLACE, Role.COLLECT),
    (Role.STORAGE, Role.COLLECT),  # 넓게 등록한 ③ 안의 저장 위치 → ③ 스캔에서 빠짐
}


def _overlap_conflict(p: Path, role: Role, existing: list[RegisteredRoot]) -> bool:
    for r in existing:
        if r.path == p:
            return True  # 같은 경로 2역할
        if is_within(p, r.path):  # 새 것이 안쪽
            if (role, r.role) not in _ALLOWED_NESTING:
                return True
        elif is_within(r.path, p):  # 새 것이 바깥쪽
            if (r.role, role) not in _ALLOWED_NESTING:
                return True
    return False


def can_save(check: RootCheck, acks: list[Code]) -> bool:
    """REFUSE는 저장 불가. WARN은 모든 경고 코드가 acks에 있어야 저장 (R-16.13)."""
    if check.decision == Decision.REFUSE:
        return False
    return all(c in acks for c in check.warn_codes)


# ---------------- 자동 후보 탐지 (R-16.8: 등록 전 유일한 읽기 예외) ----------------

@dataclass
class RootCandidate:
    path: Path
    path_display: str
    suggested_role: Role
    reason: str
    is_git: bool = False
    md_count: int = 0


_YEAR = re.compile(r"(20\d{2})")
_SKIP_DIRS = {".git", "node_modules", "build", "dist", "target", ".venv", "venv", "__pycache__"}


def _count_md(root: Path, max_depth: int = 4, cap: int = 5000) -> int:
    """이름만 센다(본문 0바이트). .git·빌드 폴더 제외, 링크 미추적."""
    n = 0
    base_depth = len(root.parts)
    for cur, dirs, files in os.walk(root, followlinks=False):
        depth = len(Path(cur).parts) - base_depth
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS] if depth < max_depth else []
        if os.name == "nt":
            dirs[:] = [d for d in dirs if not (Path(cur) / d).is_junction()]
        n += sum(1 for f in files if f.lower().endswith(".md"))
        if n >= cap:
            break
    return n


def discover_candidates(home: Path, now: datetime | None = None) -> list[RootCandidate]:
    now = now or datetime.now()
    home = real(home)
    out: list[RootCandidate] = []
    docs = home / "Documents"

    def add(p: Path, role: Role, reason: str, **kw) -> None:
        if p.is_dir() and not p.is_symlink():
            out.append(RootCandidate(real(p), tilde(real(p), home), role, reason, **kw))

    # 과제 폴더 ~/Documents/ws* 1단계
    if docs.is_dir():
        for d in sorted(docs.iterdir()):
            if not (d.is_dir() and not d.is_symlink() and d.name.startswith("ws")):
                continue
            m = _YEAR.search(d.name)
            recent = datetime.fromtimestamp(d.stat().st_mtime) > now - timedelta(days=30)
            current = (m and int(m.group(1)) >= now.year) or (not m and recent)
            add(
                d,
                Role.CURRENT if current else Role.BASELINE,
                "올해 과제로 보임" if current else "과거 과제로 보임",
                is_git=(d / ".git").exists(),
            )

    # 흩어진 곳
    for rel, reason in (
        ("Desktop", "바탕화면"),
        ("Downloads", "다운로드"),
        ("Documents/카카오톡 받은 파일", "카카오톡 받은 파일"),
        ("카카오톡 받은 파일", "카카오톡 받은 파일"),
    ):
        add(home / rel, Role.COLLECT, reason)

    # AI 협업 저장소: md ≥ 10개인 git 저장소
    seen = {c.path for c in out}
    bases = [home / b for b in ("dev", "workspace", "projects", "src")]
    repo_dirs: list[Path] = []
    for b in bases:
        if b.is_dir():
            repo_dirs += [d for d in sorted(b.iterdir()) if d.is_dir() and not d.is_symlink()]
    if docs.is_dir():
        for d in sorted(docs.iterdir()):
            if d.is_dir() and not d.is_symlink():
                repo_dirs.append(d)
                repo_dirs += [x for x in sorted(d.iterdir()) if x.is_dir() and not x.is_symlink()] if os.access(d, os.R_OK) else []
    for d in repo_dirs:
        if not (d / ".git").exists() or real(d) in seen:
            continue
        n = _count_md(d)
        if n >= 10:
            add(d, Role.IN_PLACE, f"AI 협업 문서가 많은 저장소 (Markdown 문서 {n}개)", is_git=True, md_count=n)
            seen.add(real(d))

    # ⑤ 저장 위치 기본 제안 (없으면 만들 위치만 제안 — 실제 생성은 확정 후)
    storage = docs / STORAGE_DEFAULT_NAME
    out.append(
        RootCandidate(
            storage if not storage.exists() else real(storage),
            tilde(storage, home),
            Role.STORAGE,
            "보류함·지식센터·내보내기를 모을 위치 (기본 제안)",
        )
    )
    return out

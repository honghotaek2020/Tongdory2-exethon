"""① 스캔 (02_정책 §2, R-1.13, §3.1 EX_GIT·EX_BUILD·EX_REPO).

- lstat·no-follow만. 링크는 따라가지 않고 링크 자체를 `is_link=True` 항목으로 돌려준다.
- ③ COLLECT: 1단계 파일 + 하위 폴더 파일. 프로젝트 폴더는 통째로 `is_dir=True` 항목 1개(내부 미탐색).
- ② CURRENT: 루트 직하 또는 `02 doc` 아래 어디든 있는 엉망 폴더(R-1.12) **안의 파일만**.
- `.git`·빌드 폴더는 들어가지 않는다. 다른(더 깊은) 등록 루트는 건너뛴다.
- 디렉터리 목록을 읽기 전과 항목마다 `scope.check(path, READ)`를 통과해야 한다.
"""

from __future__ import annotations

import heapq
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from tongdory2.fsx.paths import is_link_stat, nfc
from tongdory2.fsx.roots import RegisteredRoot, Role, is_denied_path
from tongdory2.fsx.scope import Access, Scope
from tongdory2.spec.normalize import compare_key, is_messy, parse

BUILD_DIRS = {
    "node_modules", "build", "dist", "target", "out", ".gradle", ".idea", ".vscode", "venv", ".venv",
    "__pycache__", ".next", "Pods", "DerivedData", "vendor", ".terraform",
}
SKIP_DIRS = BUILD_DIRS | {".git", ".svn", ".hg"}
PROJECT_MARKERS = (
    "package.json", "pom.xml", "Cargo.toml", "go.mod", "pyproject.toml", "setup.py", "composer.json",
    "Gemfile", "mix.exs", "CMakeLists.txt",
)
PROJECT_MARKER_PREFIXES = ("build.gradle", "settings.gradle")  # build.gradle.kts 포함
PROJECT_MARKER_SUFFIXES = (".xcodeproj", ".xcworkspace", ".sln", ".csproj")
AI_CONTEXT_DIRS = {
    ".claude", ".cursor", ".agent", ".agents", ".agile", ".windsurf", ".codex", ".roo", ".kiro", ".continue",
}

MAX_DEPTH = 8
MAX_ITEMS = 20000


@dataclass
class ScanCoverage:
    limited: bool = False
    errors: int = 0
    found: int = 0
    pending: int = 0


@dataclass
class ScanItem:
    path: Path
    root_id: str
    role: Role
    rel: str
    is_dir: bool
    size: int
    mtime: float
    is_link: bool
    mtime_ns: int = 0
    device: int = 0
    inode: int = 0
    ctime_ns: int = 0

    @property
    def name(self) -> str:
        return self.path.name


def _marker_in(names: set[str]) -> str | None:
    """이름 목록에 프로젝트 마커가 있으면 그 이름 (project_marker와 같은 규칙)."""
    return _marker(sorted(names), names)


def project_marker(d: Path) -> str | None:
    """폴더가 프로젝트 폴더면 첫 마커 이름(`.git` 우선), 아니면 None. 이름만 본다(lstat)."""
    try:
        names = os.listdir(d)
    except OSError:
        return None
    return _marker(sorted(names), set(names))


def _marker(names: list[str], names_set: set[str]) -> str | None:
    if ".git" in names_set:  # 디렉터리든 파일(worktree/submodule)이든
        return ".git"
    for m in PROJECT_MARKERS:
        if m in names_set:
            return m
    for n in names:
        if n.startswith(PROJECT_MARKER_PREFIXES) or n.endswith(PROJECT_MARKER_SUFFIXES):
            return n
    if "Makefile" in names_set and "src" in names_set:
        return "Makefile+src"
    return None


def _is_ai_context_dir(name: str) -> bool:
    low = name.lower()
    return low in AI_CONTEXT_DIRS or low.startswith(".aider")


def collect_boundary(p: Path) -> bool:
    """수집 시 하위 파일을 읽지 않는 폴더인지 확인한다."""
    name = p.name
    return name in SKIP_DIRS or name.endswith(".app") or _is_ai_context_dir(name) or name.startswith(".") or bool(project_marker(p))


class _Walker:
    def __init__(self, root: RegisteredRoot, scope: Scope, accept: Callable[[ScanItem], None],
                 coverage: ScanCoverage, visit_limit: int | None = None,
                 tick: Callable[[int, int], None] | None = None) -> None:
        self.root = root
        self.tick = tick  # 맨 위 항목 기준 진행 (지난 개수, 전체 개수) — 전체 파일 수를 몰라도 %를 낼 수 있다
        self.scope = scope
        self.accept = accept
        self.coverage = coverage
        self.remaining = visit_limit

    def _own(self, p: Path) -> bool:
        owner = self.scope.owner(p)
        return owner is not None and owner.root_id == self.root.root_id

    def _item(self, p: Path, st: os.stat_result, is_dir: bool, is_link: bool) -> None:
        rel = nfc(p.relative_to(self.root.path).as_posix())
        self.accept(ScanItem(p, self.root.root_id, self.root.role, rel, is_dir,
                             0 if is_dir else st.st_size, st.st_mtime, is_link, st.st_mtime_ns,
                             st.st_dev, st.st_ino, st.st_ctime_ns))

    def _top(self, entries, depth: int):
        """맨 위(depth 0) 항목은 목록을 먼저 만들고 하나씩 지날 때마다 tick을 부른다."""
        if depth or not self.tick:
            yield from entries
            return
        entries = list(entries)
        for i, entry in enumerate(entries):
            self.tick(i, len(entries))
            yield entry
        self.tick(len(entries), len(entries))

    def _entries(self, d: Path, checked: bool = False) -> Iterator[tuple[Path, os.stat_result]]:
        if not checked and not self.scope.check(d, Access.READ).allowed:
            return
        try:
            with os.scandir(d) as entries:
                for entry in entries:
                    if self.remaining is not None:
                        if self.remaining == 0:
                            self.coverage.limited = True
                            return
                        self.remaining -= 1
                    try:
                        # Windows DirEntry.stat()은 파일 ID를 0으로 돌려준다. 묶음 재검사와 같은 방식으로 읽는다.
                        info = os.lstat(entry.path) if os.name == "nt" else entry.stat(follow_symlinks=False)
                        yield d / nfc(entry.name), info
                    except OSError:
                        self.coverage.errors += 1
        except OSError:
            self.coverage.errors += 1

    def _visit(self, p: Path, st: os.stat_result) -> str | None:
        """공통 판정: 'link' | 'file' | 'dir' | None(건너뜀).

        파일·링크는 다시 검사하지 않는다: 부모 폴더가 `_entries`에서 범위·링크·거부 경로 검사를 통과했고,
        거부 규칙은 모두 폴더 경로 기준이며 등록 루트는 폴더라서 파일의 소유 루트는 부모와 같다.
        (파일마다 경로 구성요소 lstat + realpath를 하던 비용이 20만 개에서 탐색 시간의 대부분이었다)
        """
        if stat.S_ISREG(st.st_mode):
            return "file"
        if is_link_stat(st):
            return "link"
        if not self._own(p):
            return None  # 더 깊은 등록 루트 또는 범위 밖
        # 폴더: 부모는 이미 범위·링크 검사를 통과했고 p 자신은 lstat 기준 링크가 아니므로
        # 새로 확인할 것은 거부 경로(시스템·Library·비밀 폴더·앱 데이터)뿐이다.
        if is_denied_path(p, self.scope.home, self.scope.app_data):
            return None  # 거부 경로 — 항목으로도 남기지 않는다
        if stat.S_ISDIR(st.st_mode):
            return "dir"
        if stat.S_ISREG(st.st_mode):
            return "file"
        return None  # 소켓·FIFO 등

    def collect(self, d: Path, depth: int, take_files: bool = True,
                listed: list[tuple[Path, os.stat_result]] | None = None) -> None:
        """COLLECT 규칙으로 d 아래를 훑는다. 폴더 목록은 한 번만 읽는다(경계 판정과 재귀에 같이 쓴다)."""
        for p, st in self._top(listed if listed is not None else self._entries(d), depth):
            kind = self._visit(p, st)
            if kind == "link":
                self._item(p, st, False, True)
            elif kind == "file":
                if take_files:
                    self._item(p, st, False, False)
            elif kind == "dir":
                name = p.name
                if name in SKIP_DIRS:
                    continue
                if name.endswith(".app") or _is_ai_context_dir(name) or name.startswith("."):
                    self._item(p, st, True, False)  # 통째 항목, 내부 미탐색
                    continue
                children = list(self._entries(p, checked=True))  # _visit에서 이미 범위 검사 통과
                if _marker_in({c.name for c, _ in children}):
                    self._item(p, st, True, False)  # 프로젝트 폴더: 통째 항목
                    continue
                if depth < MAX_DEPTH:
                    self.collect(p, depth + 1, take_files, children)
                else:
                    self.coverage.limited = True

    def current(self, d: Path, depth: int, under_doc: bool) -> None:
        """CURRENT 규칙: 엉망 폴더 안 파일만. 루트 직하와 `02 doc` 아래에서 엉망 폴더를 찾는다."""
        for p, st in self._top(self._entries(d), depth):
            kind = self._visit(p, st)
            if kind != "dir":
                continue  # 엉망 폴더 밖 파일·링크는 대상 아님
            name = p.name
            if name in SKIP_DIRS or name.startswith("."):
                continue
            if is_messy(name):
                self.collect(p, depth + 1)
            elif depth < MAX_DEPTH and (under_doc or (depth == 0 and compare_key(parse(name).name) == "doc")):
                self.current(p, depth + 1, True)


def scan_collect(roots: list[RegisteredRoot], scope: Scope, *, include_current: bool = True,
                 coverage: ScanCoverage | None = None, batch_size: int | None = None,
                 processed: set[tuple[str, int, int]] | None = None,
                 progress: Callable[[int], None] | None = None,
                 on_item: Callable[[ScanItem], None] | None = None,
                 visit_limit: int | None = None,
                 on_fraction: Callable[[float], None] | None = None) -> list[ScanItem]:
    """전체 경로를 순회하고 이번 묶음에 필요한 최신 항목만 메모리에 보관한다."""
    out: list[ScanItem] = []
    newest: list[tuple[int, str, ScanItem]] = []
    coverage = coverage if coverage is not None else ScanCoverage()
    processed = processed if processed is not None else set()

    def accept(item: ScanItem) -> None:
        coverage.found += 1
        if progress and coverage.found % 128 == 0:
            progress(coverage.found)
        if on_item is not None:
            on_item(item)
            return
        path = str(item.path)
        if (path, item.size, item.mtime_ns) in processed:
            return
        coverage.pending += 1
        if batch_size is not None:
            ranked = (item.mtime_ns, path, item)
            if len(newest) < batch_size:
                heapq.heappush(newest, ranked)
            elif ranked[:2] > newest[0][:2]:
                heapq.heapreplace(newest, ranked)
        elif len(out) < MAX_ITEMS:
            out.append(item)
        else:
            coverage.limited = True

    roles = (Role.COLLECT, Role.CURRENT) if include_current else (Role.COLLECT,)
    targets = [r for r in roots if r.role in roles]
    for index, root in enumerate(targets):
        if not scope.check(root.path, Access.READ).allowed:
            coverage.errors += 1
            continue
        tick = None
        if on_fraction:
            def tick(done, total, index=index):
                on_fraction((index + (done / total if total else 1)) / len(targets))
        walker = _Walker(root, scope, accept, coverage, visit_limit, tick)
        if root.role == Role.COLLECT:
            walker.collect(root.path, 0)
        else:
            walker.current(root.path, 0, False)
    if progress:
        progress(coverage.found)
    if batch_size is not None:
        return [entry[2] for entry in sorted(newest, key=lambda entry: entry[:2], reverse=True)]
    return sorted(out, key=lambda item: str(item.path))

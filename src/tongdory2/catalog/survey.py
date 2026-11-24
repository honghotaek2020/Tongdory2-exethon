"""파일 종류 살펴보기 — DB에 저장하기 전에 확장자·몰린 폴더를 집계해 사용자가 제외할 수 있게 한다.

파일 내용은 읽지 않는다. 폴더를 한 번 훑으며 이름·경로만 센다. 걸러낸 뒤 남은 항목만 목록(DB)에 저장한다.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from tongdory2.fsx.scan import ScanCoverage, ScanItem, scan_collect
from tongdory2.fsx.scope import Scope

FOLDER = "(폴더)"
NO_EXT = "(확장자 없음)"
# 인증서·키는 민감 파일로 판정되어 어차피 옮기지 않는다 — 처음부터 제외해 두면 검사 시간이 준다.
SENSITIVE_EXTS = {"pem", "key", "p12", "pfx", "jks", "keystore", "der", "crt", "cer", "p8", "ppk"}
MAX_DEPTH = 4          # 몰린 폴더를 찾을 때 볼 최대 깊이
TOP_EXTS = 12          # 크게 보여 줄 확장자 수
CONCENTRATION = 0.8    # 상위 폴더 1~2개가 이 비율 이상이면 "몰린 폴더"로 본다

_SCHEMA = "CREATE TABLE IF NOT EXISTS scan_filters(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)"


def ext_of(item: ScanItem) -> str:
    if item.is_dir:
        return FOLDER
    name = item.path.name
    if "." not in name.lstrip("."):
        return NO_EXT
    return name.rsplit(".", 1)[-1].lower()


def _dir_parts(item: ScanItem) -> list[str]:
    parts = item.rel.split("/")
    return parts if item.is_dir else parts[:-1]


@dataclass
class Exclusions:
    exts: set[str] = field(default_factory=set)
    dirs: set[str] = field(default_factory=set)  # "root_id|상대경로"

    def keep(self, item: ScanItem) -> bool:
        if ext_of(item) in self.exts:
            return False
        rel = item.rel
        for d in self.dirs:
            root_id, prefix = d.split("|", 1)
            if root_id == item.root_id and (rel == prefix or rel.startswith(prefix + "/")):
                return False
        return True

    def to_json(self) -> str:
        return json.dumps({"exts": sorted(self.exts), "dirs": sorted(self.dirs)}, ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str | None) -> "Exclusions":
        d = json.loads(s) if s else {}
        return cls(set(d.get("exts", [])), set(d.get("dirs", [])))


class FilterStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        conn.execute(_SCHEMA)

    def load(self) -> Exclusions | None:
        row = self.conn.execute("SELECT body FROM scan_filters WHERE id=1").fetchone()
        return Exclusions.from_json(row[0]) if row else None

    def save(self, ex: Exclusions) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO scan_filters VALUES(1, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                              (ex.to_json(),))


@dataclass
class Survey:
    total: int
    exts: list[dict]        # [{ext, count, sensitive, folders:[(경로, 수)], depth}]
    hotspots: list[dict]    # [{key, path, count, share}] — 여러 확장자가 몰린 폴더 제외 제안
    errors: int
    roots: dict[str, str]   # root_id → 표시 이름


def _concentrated(counter_by_depth: list[Counter], total: int) -> tuple[int, list[tuple[str, int]]]:
    """상위 1~2개 폴더가 전체의 80% 이상을 차지하는 가장 깊은 층을 고른다(없으면 1층)."""
    best = 1
    for depth in range(1, MAX_DEPTH + 1):
        c = counter_by_depth[depth - 1]
        top = c.most_common(2)
        if top and top[0][0] and sum(n for _, n in top) >= CONCENTRATION * total:
            best = depth
    return best, counter_by_depth[best - 1].most_common(3)


def summarize(items: list[ScanItem], errors: int, root_names: dict[str, str]) -> Survey:
    by_ext: Counter = Counter()
    ext_depth: dict[str, list[Counter]] = {}
    all_depth = [Counter() for _ in range(MAX_DEPTH)]
    for it in items:
        e = ext_of(it)
        by_ext[e] += 1
        parts = _dir_parts(it)
        levels = ext_depth.setdefault(e, [Counter() for _ in range(MAX_DEPTH)])
        for d in range(1, MAX_DEPTH + 1):
            key = f"{it.root_id}|{'/'.join(parts[:d])}"
            levels[d - 1][key] += 1
            all_depth[d - 1][key] += 1
    total = len(items)
    exts = []
    for e, n in by_ext.most_common():
        depth, folders = _concentrated(ext_depth[e], n)
        exts.append({"ext": e, "count": n, "sensitive": e in SENSITIVE_EXTS, "depth": depth,
                     "folders": [(_label(k, root_names), c) for k, c in folders]})
    hotspots = []
    if total >= 1000:
        depth, top = _concentrated(all_depth, total)
        for i, (k, c) in enumerate(top[:2]):
            # 1위는 전체의 10% 이상, 2위는 1,000개 이상이면 제안 (1위를 빼면 2위가 대부분이 되는 경우가 많다)
            if k.split("|", 1)[1] and (c >= 0.1 * total if i == 0 else c >= 1000):
                hotspots.append({"key": k, "path": _label(k, root_names), "count": c, "share": c / total, "exts": {}})
    # 몰린 폴더 안의 확장자별 개수 — 확장자와 폴더를 함께 제외할 때 남는 수를 정확히 계산하려고
    if hotspots:
        for it in items:
            for h in hotspots:
                root_id, prefix = h["key"].split("|", 1)
                if it.root_id == root_id and (it.rel == prefix or it.rel.startswith(prefix + "/")):
                    e = ext_of(it)
                    h["exts"][e] = h["exts"].get(e, 0) + 1
                    break
    return Survey(total, exts, hotspots, errors, root_names)


def _label(key: str, root_names: dict[str, str]) -> str:
    root_id, rel = key.split("|", 1)
    return rel or f"{root_names.get(root_id, '')} (맨 위)"


def run_survey(roots, scope: Scope, include_current: bool, progress=None,
               on_fraction=None) -> tuple[Survey, list[ScanItem]]:
    """폴더를 한 번 훑어 항목을 메모리에 모으고 종류별로 센다 (DB 쓰기 없음)."""
    items: list[ScanItem] = []
    coverage = ScanCoverage()
    scan_collect(roots, scope, include_current=include_current, coverage=coverage, progress=progress,
                 on_item=items.append, on_fraction=on_fraction)
    names = {r.root_id: r.path.name for r in roots}
    return summarize(items, coverage.errors, names), items

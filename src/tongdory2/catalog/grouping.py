"""기준 폴더에 맞지 않은 파일을 로컬에서 묶어 새 폴더를 제안한다 (#47, 외부 전송 0).

단계 (비용이 적은 것부터):
  1. 기준 폴더 합류 — pipeline.prepare가 이미 처리
  2. 원래 폴더 묶음 — 같은 원래 폴더(없으면 그 위 폴더)에 GROUP_MIN개 이상 → `자동분류/<원래 폴더>`
  3. 파일 이름 묶음 — 이름 앞부분이 같은 파일 GROUP_MIN개 이상 → `자동분류/<이름 앞부분>`
  4. 남은 낱개 → `자동분류/기타` (verdict ETC, 기본 체크 해제, Liner 분석 요청 가능)

새 폴더 이름이 이미 있는 폴더·기준 폴더·다른 묶음과 겹치면 `이름 (1)`, `이름 (2)`처럼 번호를 붙인다.
한 번 정한 묶음 → 폴더 경로는 DB(group_folders)에 남겨 다음 묶음·다시 분석에서도 같은 폴더를 쓴다
(앞 묶음을 실행해 폴더가 생긴 뒤에도 `(1)`로 갈라지지 않게).
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from tongdory2.catalog.pipeline import auto_move
from tongdory2.fsx.paths import nfc
from tongdory2.spec.normalize import is_messy

AUTO_DIR = "자동분류"
ETC_NAME = "기타"
GROUP_MIN = 5
GROUPABLE = {"LOCAL_NO_MATCH", "LOCAL_UNSUPPORTED", "IMAGE_NOT_ANALYZED"}
BACKUP_WORDS = ("backup", "bak", "백업", "old", "이전", "사본")
_SPLIT = re.compile(r"[\s_\-.()\[\]{}·,~+=#@!]+")
_BAD = re.compile(r'[/\\:*?"<>|\x00-\x1f]+')
_SCHEMA = """CREATE TABLE IF NOT EXISTS group_folders(
    project TEXT NOT NULL, key TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY(project, key))"""


def name_prefix(filename: str) -> str | None:
    """파일 이름의 첫 의미 있는 단어 (날짜·번호·버전은 건너뜀, 끝 숫자는 뗀다)."""
    stem = filename.rsplit(".", 1)[0] if "." in filename.lstrip(".") else filename
    for tok in _SPLIT.split(nfc(stem)):
        if not tok or tok.isdigit() or re.fullmatch(r"[vV]\d+|rev\d+|r\d+|\d+차|최종|final|copy|복사본", tok, re.I):
            continue
        tok = re.sub(r"\d+$", "", tok)
        if len(tok) >= 2:
            return tok[:30]
    return None


def _clean(seg: str) -> str:
    seg = _BAD.sub("_", nfc(seg)).strip(" .") or "이름없음"
    # 'OLD'·'backup'·'temp' 같은 이름은 실행 안전 검사에서 '정리 필요 폴더'로 보고 목적지로 막는다 → 보관 폴더 이름으로
    return f"{seg}_보관" if is_messy(seg) else seg


def _clean_path(path: str) -> str:
    return "/".join(_clean(p) for p in path.split("/") if p)


def is_backup(rel_dir: str) -> bool:
    low = rel_dir.lower()
    return any(w in part for part in low.split("/") for w in BACKUP_WORDS)


class FolderNames:
    """묶음 키 → 새 폴더 경로. 겹치면 (1)·(2) 번호."""

    def __init__(self, conn: sqlite3.Connection | None, project: Path, spec_paths: set[str]) -> None:
        self.conn, self.project = conn, str(project)
        self.root = project
        self.spec_paths = {p.casefold() for p in spec_paths}
        self.saved: dict[str, str] = {}
        if conn is not None:
            conn.execute(_SCHEMA)
            self.saved = dict(conn.execute("SELECT key, path FROM group_folders WHERE project=?", (self.project,)))
        self.taken = {p.casefold(): k for k, p in self.saved.items()}
        self.new: dict[str, str] = {}

    def _free(self, path: str, key: str) -> bool:
        cf = path.casefold()
        owner = self.taken.get(cf)
        if owner is not None:
            return owner == key
        if cf in self.spec_paths:
            return False
        return not (self.root / path).exists()

    def path_for(self, key: str, wanted: str) -> str:
        if key in self.saved:
            return _clean_path(self.saved[key])  # 예전에 저장한 경로에 막히는 이름이 있으면 고쳐 쓴다
        parent, _, leaf = wanted.rpartition("/")
        path, i = wanted, 0
        while not self._free(path, key):
            i += 1
            path = f"{parent}/{leaf} ({i})" if parent else f"{leaf} ({i})"
        self.saved[key] = path
        self.taken[path.casefold()] = key
        self.new[key] = path
        return path

    def commit(self) -> None:
        if self.conn is None or not self.new:
            return
        with self.conn:
            self.conn.executemany("INSERT OR IGNORE INTO group_folders VALUES(?,?,?)",
                                  [(self.project, k, p) for k, p in self.new.items()])
        self.new.clear()


def _groupable(e, item) -> bool:
    return (e.verdict == "HOLD" and e.reason_code in GROUPABLE and not e.screenshot_batch
            and e.sensitivity != "L3" and not item.is_dir and not item.is_link)


def apply_groups(entries: list, items: list, project: Path, spec_nodes: list[dict],
                 conn: sqlite3.Connection | None = None, minimum: int = GROUP_MIN) -> dict:
    """entries를 제자리에서 바꾼다. 반환: 단계별 개수."""
    spec_paths = {n["display_path"] for n in spec_nodes if n.get("status") == "ADOPTED"}
    names = FolderNames(conn, project, spec_paths)
    todo = [(e, it) for e, it in zip(entries, items) if _groupable(e, it)]
    stats = {"folder": 0, "name": 0, "etc": 0, "groups": 0}

    made: dict[str, dict] = {}

    def assign(members, key: str, wanted: str, kind: str, label: str, why: str) -> None:
        path = names.path_for(key, wanted)
        made.setdefault(key, {"key": key, "path": path, "why": why})
        stats["groups"] += 1
        for e, it in members:
            e.verdict, e.reason_code = "GROUP", f"GROUP_{kind.upper()}"
            e.node_path, e.new_node_name = path, path.rsplit("/", 1)[-1]  # 같은 이름이 부딪치면 _split_same_names가 나눈다
            e.group_key, e.group_label, e.group_kind = key, label, kind
            e.reason = why
            e.default_checked = auto_move(e)
            e.candidates = []
            stats[kind if kind in stats else "folder"] += 1

    # 2단계: 원래 폴더 — 가장 깊은 폴더부터, 모자라면 한 단계 위로 모아 본다
    cand = {}
    for e, it in todo:
        parts = it.rel.split("/")[:-1]
        if parts:
            cand[id(e)] = (it.root_id, parts)
    left = [(e, it) for e, it in todo if id(e) in cand]
    while left:
        by = defaultdict(list)
        for e, it in left:
            root_id, parts = cand[id(e)]
            by[(root_id, "/".join(parts))].append((e, it))
        nxt = []
        for (root_id, rel_dir), members in by.items():
            if f"folder:{root_id}|{rel_dir}" in made:  # 하위 폴더 낱개가 이미 만든 위 폴더 묶음에 합류
                first = made[f"folder:{root_id}|{rel_dir}"]
                assign(members, first["key"], first["path"], "folder", rel_dir, first["why"])
                stats["groups"] -= 1
            elif f"folder:{root_id}|{rel_dir}" in names.saved:  # #75 앞서 만든 묶음 폴더가 있으면 적어도 합류
                key = f"folder:{root_id}|{rel_dir}"
                assign(members, key, names.saved[key], "folder", rel_dir,
                       f"원래 폴더 '{rel_dir}' — 앞서 만든 묶음 폴더에 합류")
            elif len(members) >= minimum:
                backup = is_backup(rel_dir)
                wanted = f"{AUTO_DIR}/{'백업 사본/' if backup else ''}" + "/".join(_clean(p) for p in rel_dir.split("/"))
                assign(members, f"folder:{root_id}|{rel_dir}", wanted, "folder", rel_dir,
                       f"원래 폴더 '{rel_dir}'에 함께 있던 {len(members)}개 — 같은 이름의 새 폴더로 묶음"
                       + (" (백업 폴더로 보임)" if backup else ""))
            else:
                for e, it in members:
                    root_id, parts = cand[id(e)]
                    if len(parts) > 1:
                        cand[id(e)] = (root_id, parts[:-1])
                        nxt.append((e, it))
        left = nxt

    # 3단계: 파일 이름 앞부분
    by_name = defaultdict(list)
    for e, it in todo:
        if e.verdict == "HOLD" and (p := name_prefix(it.path.name)):
            by_name[p.casefold()].append((e, it, p))
    for key, members in sorted(by_name.items(), key=lambda kv: -len(kv[1])):
        if len(members) >= minimum:
            label = members[0][2]
            assign([(e, it) for e, it, _ in members], f"name:{key}", f"{AUTO_DIR}/{_clean(label)}", "name", label,
                   f"이름이 '{label}'(으)로 시작하는 파일 {len(members)}개 — 같은 이름의 새 폴더로 묶음")

    # 4단계 앞: 남은 낱개는 기타 (기본 체크 해제 — Liner에 묻거나 기타로 보내기를 사용자가 고른다)
    rest = [(e, it) for e, it in todo if e.verdict == "HOLD"]
    if rest:
        path = names.path_for("etc", f"{AUTO_DIR}/{ETC_NAME}")
        for e, _it in rest:
            e.verdict, e.reason_code = "ETC", "GROUP_ETC"
            e.node_path, e.new_node_name = path, ETC_NAME
            e.group_key, e.group_label, e.group_kind = "etc", ETC_NAME, "etc"
            e.reason = "어느 폴더·이름 묶음에도 들지 않은 낱개 파일 — Liner에 묻거나 기타 폴더로"
            e.default_checked = False
        stats["etc"] = len(rest)
    _split_same_names([(e, it) for e, it in todo if e.verdict in ("GROUP", "ETC")])
    names.commit()
    return stats


def _split_same_names(pairs) -> None:
    """같은 목적지에 같은 이름 파일이 둘 이상이면 원래 상위 폴더 이름으로 한 단계씩 나눈다.
    파일 이름은 바꾸지 않는다(이름 묶음·기타에서 '요약.md'가 여러 폴더에서 모일 때)."""
    depth = {id(e): 0 for e, _ in pairs}
    base = {id(e): e.node_path for e, _ in pairs}
    for _ in range(4):
        seen = defaultdict(list)
        for e, it in pairs:
            seen[(e.node_path.casefold(), e.name.casefold())].append((e, it))
        clashes = [g for g in seen.values() if len(g) > 1]
        if not clashes:
            return
        for group in clashes:
            for e, it in group:
                parents = it.rel.split("/")[:-1]
                depth[id(e)] += 1
                tail = parents[-depth[id(e)]:] if parents else []
                if tail:
                    e.node_path = base[id(e)] + "/" + _clean_path("/".join(tail))


def organize_rate(verdicts: dict) -> dict:
    """정리율 = 옮길 곳이 정해진 파일 / 정리 대상(일부러 그대로 두는 EXCLUDE 제외)."""
    total = sum(verdicts.values())
    excluded = verdicts.get("EXCLUDE", 0)
    target = total - excluded
    placed = sum(verdicts.get(v, 0) for v in ("JOIN", "SPEC_NODE", "NEW_NODE", "GROUP", "DUP"))
    etc = verdicts.get("ETC", 0)
    hold = verdicts.get("HOLD", 0)
    pct = round(100 * placed / target) if target else 0
    with_etc = round(100 * (placed + etc) / target) if target else 0
    return {"pct": pct, "with_etc": with_etc, "placed": placed, "target": target, "etc": etc, "hold": hold,
            "excluded": excluded, "grouped": verdicts.get("GROUP", 0)}

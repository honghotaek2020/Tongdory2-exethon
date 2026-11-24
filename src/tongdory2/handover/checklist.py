"""K-01 누락 산출물 체크리스트 — 확정 명세 기준으로 현재 과제 폴더(+ 제자리 목록화 결과)를 대조. 읽기만 한다."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from tongdory2.fsx.scope import Access, Scope

DRAFT = re.compile(r"(초안|draft|작성\s*중|작성중|wip|임시)", re.I)


@dataclass
class CheckRow:
    node_id: str
    path: str
    label: str
    required: bool
    status: str  # PRESENT | DRAFT_ONLY | PRESENT_IN_PLACE | MISSING
    files: list[str] = field(default_factory=list)
    in_place: list[str] = field(default_factory=list)
    note: str = ""


STATUS_LABEL = {"PRESENT": "있음", "DRAFT_ONLY": "초안만", "PRESENT_IN_PLACE": "AI 협업 문서로만 있음", "MISSING": "없음"}


def _files(d: Path, scope: Scope) -> list[str]:
    if not d.is_dir() or not scope.check(d, Access.READ).allowed:
        return []
    try:
        return sorted(e.name for e in os.scandir(d) if e.is_file(follow_symlinks=False) and not e.name.startswith("."))
    except OSError:
        return []


def build_checklist(spec: dict, current_root: Path, inplace_entries: list[dict], scope: Scope) -> list[CheckRow]:
    nodes = [n for n in spec["body"]["nodes"] if n["status"] == "ADOPTED" and n["role"] == "DEST"
             and n["profile"] in ("common", "build") and n["node_id"] != "src"]
    ip_by_node: dict[str, list[dict]] = {}
    for e in inplace_entries:
        if e.get("node_id") and e.get("verdict") in ("JOIN",) and not e.get("backup_of"):
            ip_by_node.setdefault(e["node_id"], []).append(e)
    rows: list[CheckRow] = []
    for n in sorted(nodes, key=lambda x: x["display_path"]):
        files = _files(current_root / n["display_path"], scope)
        ips = ip_by_node.get(n["node_id"], [])
        if files:
            status = "DRAFT_ONLY" if all(DRAFT.search(f) for f in files) else "PRESENT"
        elif ips:
            status = "PRESENT_IN_PLACE"
        else:
            status = "MISSING"
        rows.append(CheckRow(n["node_id"], n["display_path"], n["label"], bool(n.get("required_for_handover")), status,
                             files, [e.get("title") or e["name"] for e in ips]))
    # 진입점 파일 (산출물목록의 README·업무인수인계)
    for ent in spec["body"].get("entries", []):
        name = ent["name"]
        exists = (current_root / name).is_file()
        nid = "entry.handover" if "인수인계" in name else "entry.readme" if name.lower().startswith("readme") else f"entry.{len(rows)}"
        ips = ip_by_node.get(nid, [])
        if exists:
            status = "PRESENT"
        elif ips:
            status = "DRAFT_ONLY" if all(DRAFT.search((e.get("title") or "") + e["name"]) for e in ips) else "PRESENT_IN_PLACE"
        else:
            status = "MISSING"
        rows.append(CheckRow(nid, name, name, bool(ent.get("required")), status, [name] if exists else [],
                             [e.get("title") or e["name"] for e in ips], "진입점 파일"))
    return rows

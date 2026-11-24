"""A-01 `README_문서색인.md` 렌더링 — 코드 템플릿, LLM 없음. 기존 파일은 덮어쓰지 않는다."""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import date
from pathlib import Path

from tongdory2.catalog.inplace import DOC_TYPE_LABEL

GIT_LABEL = {"TRACKED": "Git 추적", "UNTRACKED": "미추적", "IGNORED": "Git 제외(보존 위험)", "NO_REPO": "—", "UNKNOWN": "?"}
ORDER = ["PLAN", "ANALYSIS", "DESIGN", "TEST", "REPORT", "MEETING_HANDOVER", "BACKUP", "AI_INSTRUCTION", "OTHER"]


def render(entries: list[dict], summary: dict, repo_root: Path, index_dir: Path, project_name: str, today: date | None = None) -> str:
    today = today or date.today()
    lines = [
        f"# 문서 색인 — {project_name}",
        "",
        f"- 생성일: {today.isoformat()} (자동 생성, 원본 문서는 옮기거나 고치지 않음)",
        f"- 대상: `{repo_root.name}` · md {summary['md_total']}개 · **Git에 없는 문서 {summary['not_in_git']}개**"
        f" (제외 {summary['ignored']} · 미추적 {summary['untracked']}) · 이번 달 {summary['this_month']}개",
        "",
    ]
    groups: dict[str, list[dict]] = defaultdict(list)
    ai_paths: list[str] = []
    sensitive_count = 0
    for e in entries:
        if e.get("reason_code") == "SENSITIVE" or e.get("sensitivity") == "L3":
            sensitive_count += 1
            continue
        if e.get("reason_code") == "AI_CONTEXT":
            ai_paths.append(os.path.relpath(e["path"], index_dir).replace("\r", r"\r").replace("\n", r"\n"))
            continue
        if e.get("kind") != "text":
            continue
        groups[e.get("doc_type") or "OTHER"].append(e)
    for dt in ORDER:
        items = groups.get(dt)
        if not items:
            continue
        lines += [f"## {DOC_TYPE_LABEL[dt]} ({len(items)})", "", "| 문서 | 대응 산출물 | Git | 수정일 |", "|---|---|---|---|"]
        for e in sorted(items, key=lambda x: x["path"]):
            p = Path(e["path"])
            link = os.path.relpath(p, index_dir).replace(" ", "%20")
            title = (e.get("title") or p.stem).replace("|", "\\|")
            node = e.get("node_path") or "—"
            if e.get("verdict") == "DUP":
                node += " (원본과 같음)"
            lines.append(f"| [{title}]({link}) | {node} | {GIT_LABEL.get(e.get('git_status') or 'UNKNOWN', '?')} | "
                         f"{date.fromtimestamp(e.get('mtime') or 0).isoformat()} |")
        lines.append("")
    lines += ["## 일반 목록에서 제외한 문서", "", f"- AI 도구 설정 파일 {len(ai_paths)}개", f"- 민감 문서 {sensitive_count}개", ""]
    if ai_paths:
        lines += ["AI 도구 설정 파일의 상대 경로", "", *(f"    {p}" for p in sorted(ai_paths)), ""]
    lines += ["---", "이 파일은 색인용 새 파일입니다. 지워도 원본 문서에는 영향이 없습니다."]
    return "\n".join(lines) + "\n"

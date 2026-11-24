"""A-01 md 메타 추출: 머리말(frontmatter), H1, 헤딩, 링크. 파일을 수정하지 않는다."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_H = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
MAX_BYTES = 1024 * 1024


@dataclass
class MdMeta:
    title: str = ""
    frontmatter: dict[str, str] = field(default_factory=dict)
    headings: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    body: str = ""
    size: int = 0


def read_md(path: Path) -> MdMeta:
    m = MdMeta()
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(MAX_BYTES)
    except OSError:
        return m
    m.size = len(raw)
    text = raw.decode("utf-8", "replace")
    lines = text.splitlines()
    i = 0
    if lines and lines[0].strip() == "---":
        for j in range(1, min(len(lines), 60)):
            if lines[j].strip() == "---":
                for ln in lines[1:j]:
                    if ":" in ln:
                        k, v = ln.split(":", 1)
                        m.frontmatter[k.strip().lower()] = v.strip().strip("\"'")
                i = j + 1
                break
    body_lines = lines[i:]
    in_code = False
    for ln in body_lines:
        if ln.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        h = _H.match(ln)
        if h:
            if len(h.group(1)) == 1 and not m.title:
                m.title = h.group(2)
            else:
                m.headings.append(h.group(2))
    m.title = m.title or m.frontmatter.get("title", "")
    m.links = _LINK.findall(text)[:50]
    m.body = "\n".join(body_lines)
    return m

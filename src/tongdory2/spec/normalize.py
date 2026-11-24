"""폴더명 정규화 (02_정책 §1.2, R-1.3~R-1.6, R-1.12).

`01. 소스` / `01.source` / `01 source` → 같은 노드로 본다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_SPECIAL_SPACES = dict.fromkeys(map(ord, "     　"), " ")
_NUM = re.compile(r"^\s*(\d{1,3})\s*[.\-_)]?\s*(.+)$")
_KEY_STRIP = re.compile(r"[\s_\-·.]+")

# 동의어 → 정규 비교키 (R-1.3 ⑤)
SYNONYMS = {
    "source": "source", "소스": "source", "src": "source", "sources": "source",
    "doc": "doc", "docs": "doc", "문서": "doc", "document": "doc", "documents": "doc",
    "매뉴얼": "매뉴얼", "메뉴얼": "매뉴얼", "manual": "매뉴얼",
    "사용자매뉴얼": "사용자매뉴얼", "사용자메뉴얼": "사용자매뉴얼", "usermanual": "사용자매뉴얼",
    "참고": "참고", "reference": "참고", "ref": "참고", "참고자료": "참고",
    "etc": "etc", "기타": "etc",
    "회의록": "회의록", "meeting": "회의록", "meetings": "회의록", "회의": "회의록",
    "usethis": "usethis", "개발환경": "usethis",
    "로그": "로그", "log": "로그", "logs": "로그", "logfile": "로그",
}

# 엉망 폴더 (R-1.12) — 비교키 기준
_MESSY = re.compile(
    r"^(tmp\d*|temp\d*|임시|새폴더\d*|newfolder.*|untitled.*|복사본.*|.*copy|copyof.*|backup|bak|old|백업|삭제예정)$"
)

# 최상위 정규 노드 ID
TOP_IDS = {"source": "src", "doc": "doc", "usethis": "env", "etc": "etc"}


def clean(name: str) -> str:
    return unicodedata.normalize("NFC", name).translate(_SPECIAL_SPACES).strip()


@dataclass(frozen=True)
class Segment:
    original: str
    number: int | None  # 정규화된 번호 (998 → 98)
    name: str  # 번호를 뗀 이름 (원래 공백 유지)
    key: str  # 비교키 (동의어 적용)


def norm_number(n: int) -> int:
    if 990 <= n <= 999:  # 3자리 99x → 9x (R-1.3 ③)
        return n - 900
    return n


def compare_key(name: str) -> str:
    k = _KEY_STRIP.sub("", clean(name)).lower()
    return SYNONYMS.get(k, k)


def parse(original: str) -> Segment:
    s = clean(original)
    m = _NUM.match(s)
    if m and not m.group(2).strip().isdigit():
        num, name = norm_number(int(m.group(1))), m.group(2).strip()
    else:
        num, name = None, s
    return Segment(original=original, number=num, name=name, key=compare_key(name))


def is_messy(name: str) -> bool:
    return bool(_MESSY.match(compare_key(parse(name).name)))


def child_label(name: str) -> str:
    """`02 doc` 아래 표기용 이름: 공백 제거 (R-1.5 `NN.이름`)."""
    return re.sub(r"\s+", "", clean(name))


def fmt_top(number: int | None, name: str) -> str:
    return f"{number:02d} {name}" if number is not None else name


def fmt_child(number: int | None, name: str) -> str:
    return f"{number:02d}.{child_label(name)}" if number is not None else child_label(name)

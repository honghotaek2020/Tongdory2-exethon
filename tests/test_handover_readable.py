"""인수인계 초안은 사람이 읽는 문서 — 링크·URL 인코딩(%EC…) 없이, macOS 분해형(NFD) 한글도 붙여 쓴다."""

import unicodedata

from tongdory2.handover.checklist import CheckRow
from tongdory2.handover.readme import render


def test_checklist_has_no_links_or_percent_encoding():
    nfd = unicodedata.normalize("NFD", "연구협약서.pdf")
    rows = [
        CheckRow("n1", "01.착수", "착수", False, "PRESENT", files=[nfd]),
        CheckRow("n2", "01.착수/01.과제수행계획서", "과제수행계획서", True, "MISSING"),
        CheckRow("n3", "02.분석", "분석", False, "PRESENT", files=[f"문서 {i}.md" for i in range(7)]),
    ]
    text = render("test", rows, None, [], {}, None)
    assert "%E" not in text and "](" not in text
    assert "- **01.착수** — 있음 1개: 연구협약서.pdf" in text  # NFC로 붙여 쓴다
    assert "- **01.착수/01.과제수행계획서** (필수) — 없음 — 인계 전에 채워야 합니다" in text
    assert "외 2개" in text

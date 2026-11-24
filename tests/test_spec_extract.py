"""D-01 표준 추출·정규화, D-05 프로필 판정, R-1.12 엉망 폴더 제외."""

from __future__ import annotations

from pathlib import Path

import pytest

from tongdory2.spec.extract_standard import extract
from tongdory2.spec.normalize import compare_key, is_messy, parse


@pytest.mark.parametrize(
    "a,b",
    [("01 source", "01.source"), ("01.source", "01. 소스"), ("02 doc", "02. 문서"),
     ("07. 사용자 매뉴얼", "07.사용자매뉴얼"), ("99.etc", "99. 기타")],
)
def test_same_node_variants(a, b):
    assert parse(a).key == parse(b).key and parse(a).number == parse(b).number


def test_998_normalized_to_98():
    s = parse("998. 장애보고")
    assert s.number == 98 and s.key == "장애보고"


def test_nfd_and_special_space():
    import unicodedata
    nfd = unicodedata.normalize("NFD", "요구사항정의서")
    assert compare_key(nfd) == compare_key("요구사항 정의서")


@pytest.mark.parametrize("name", ["tmp", "tmp2", "새 폴더", "새 폴더 3", "New Folder", "복사본 - 회의록", "backup", "old"])
def test_messy(name):
    assert is_messy(name)


def test_not_messy():
    assert not is_messy("08.보고") and not is_messy("09.참고")


def mk(root: Path, *rels: str) -> None:
    for r in rels:
        p = root / r
        if p.suffix.lower() in {".docx", ".xlsx", ".pptx", ".pdf", ".md", ".txt"}:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
        else:
            p.mkdir(parents=True, exist_ok=True)


@pytest.fixture()
def past(tmp_path: Path) -> list[Path]:
    pa = tmp_path / "ws한빛카드_카드발급_2023"
    mk(pa, "01 source/app", "02 doc/01.착수", "02 doc/02.일정관리/01.WBS", "02 doc/03.분석/01.요구사항정의서",
       "02 doc/03.분석/02.현행분석", "02 doc/04.설계/03.인터페이스설계서", "02 doc/08.보고/회의록",
       "02 doc/09.참고", "03 use this", "02 doc/08.보고/회의록/0915_주간회의록.docx")
    pb = tmp_path / "ws누리은행_대외계유지보수"
    mk(pb, "01.source/batch", "02.doc/02.유지보수/01.공정관리", "02.doc/02.유지보수/04.작업절차서",
       "02.doc/03.TEST전문", "02.doc/04.정기점검/2024", "02.doc/998.장애보고", "99.etc/logfile")
    pc = tmp_path / "ws초록통신_eSIM개통_2025"
    mk(pc, "01. 소스", "02. 문서/01.착수", "02. 문서/03.분석/01.요구사항정의서", "02. 문서/04.설계/03.인터페이스설계서",
       "02. 문서/07. 사용자 매뉴얼", "02. 문서/09.참고", "주요파일", "tmp")
    return [pc, pb, pa]  # 최신 우선


def test_extract_profiles_and_adoption(past):
    ex = extract(past)
    assert ex.profiles == {"ws초록통신_eSIM개통_2025": "build", "ws누리은행_대외계유지보수": "maint", "ws한빛카드_카드발급_2023": "build"}
    by = {n.node_id: n for n in ex.nodes}

    # 최상위: 3개 모두 소스·문서 → 채택, 표기는 다수결
    assert by["src"].status == "ADOPTED" and by["src"].role == "PROTECTED"
    assert by["doc"].status == "ADOPTED"
    # build 프로필 N=2 → ⌈4/3⌉=2 필요
    assert by["doc.01"].status == "ADOPTED"  # PA·PC
    assert by["doc.03.01"].status == "ADOPTED"
    assert by["doc.03.02"].status == "CANDIDATE"  # PA만
    assert by["doc.03.02"].missing_in == ["ws초록통신_eSIM개통_2025"]
    assert by["doc.07"].status == "CANDIDATE"  # PC만
    # 번호 없는 하위(회의록) → 형제 순서 번호 부여
    assert "doc.08.01" in by and by["doc.08.01"].segment == "01.회의록"
    assert "0915_주간회의록.docx" in by["doc.08.01"].exemplar_filenames
    # maint 프로필 노드와 998 → 98
    assert by["m.98"].label == "장애보고"
    assert by["m.02.01"].label == "공정관리" and by["m.02"].role == "CONTAINER"
    # 허브 폴더는 후보 + 힌트, 엉망 폴더는 제외
    hubs = [n for n in ex.nodes if n.node_id.startswith("x")]
    assert hubs and hubs[0].status == "CANDIDATE" and "허브" in hubs[0].hint
    assert ex.messy["ws초록통신_eSIM개통_2025"] == ["tmp"]
    # 표기 차이표
    v = next(v for v in ex.variants if v["node_id"] == "src")
    assert set(v["observed"].values()) == {"01 source", "01.source", "01. 소스"}


def test_source_not_descended(past):
    ex = extract(past)
    assert not any(n.parent_id == "src" for n in ex.nodes)
    assert all(n.exemplar_filenames == [] for n in ex.nodes if n.node_id == "src")


def test_read_guard_respected(past):
    denied = past[1]
    ex = extract(past, allow_read=lambda p: not str(p).startswith(str(denied)))
    assert not any("ws누리은행_대외계유지보수" in n.support_projects for n in ex.nodes)


def test_compare_current_project(past, tmp_path):
    from tongdory2.spec.compare import compare

    ex = extract(past)
    cur = tmp_path / "ws한빛카드_QR결제_2026"
    mk(cur, "01 source/app", "02 doc/03.분석", "02 doc/08.보고", "02 doc/새 폴더", "02 doc/결제사양참고", "tmp")
    rows = compare(cur, ex.nodes, "build")
    st = {(r.status, r.spec_path or r.actual_path) for r in rows}
    assert ("MATCH", "01 source") in st
    assert ("MATCH", "02 doc/03.분석") in st
    assert ("MISSING", "02 doc/03.분석/01.요구사항정의서") in st
    assert ("MISSING", "02 doc/01.착수") in st
    assert ("MESSY", "02 doc/새 폴더") in st and ("MESSY", "tmp") in st
    assert ("EXTRA", "02 doc/결제사양참고") in st
    # 보호 영역 안은 비교하지 않음
    assert not any((r.actual_path or "").startswith("01 source/") for r in rows)
    # 후보(CANDIDATE) 노드는 누락으로 표시하지 않음
    assert not any(r.spec_path == "02 doc/03.분석/02.현행분석" for r in rows)


def test_workspace_mode_uses_existing_folders(tmp_path):
    ws = tmp_path / "2025년도_업무"
    mk(ws, "02. 개발관리/개발계획서.docx", "김하늘님받은자료/검토의견.docx", "2026년도_과제보고서/연말보고.pptx",
       "DB_ERD/ERD_v2.pdf", "Admin 인수인계_박민수/인수인계서.docx", "tmp", "새 폴더")
    ex = extract([ws], workspace=True)
    seg = {n.segment: n for n in ex.nodes}
    assert set(seg) == {"02. 개발관리", "김하늘님받은자료", "2026년도_과제보고서", "DB_ERD", "Admin 인수인계_박민수"}
    assert all(n.status == "ADOPTED" and n.role == "DEST" for n in ex.nodes)
    assert "사람·출처" in seg["김하늘님받은자료"].description and "사람·출처" in seg["Admin 인수인계_박민수"].description
    assert "연도" in seg["2026년도_과제보고서"].description
    assert "ERD_v2.pdf" in seg["DB_ERD"].description
    assert seg["02. 개발관리"].display_path == "02. 개발관리"  # 실제 표기 그대로
    assert sorted(ex.messy[ws.name]) == ["tmp", "새 폴더"]

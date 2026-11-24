"""fsx 로컬 파이프라인: scan → filters → sensitivity → hashing → extract → screenshots.

단위 테스트(tmp_path) + 데모 데이터셋 수락 테스트(생성기를 tmp에 돌려 _manifest.json과 대조).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from tongdory2.fsx.extract import L3_NOTICE, extract
from tongdory2.fsx.filters import REASON_ALIASES, classify, is_ai_context
from tongdory2.fsx.hashing import dup_groups, sha256_file, version_groups, version_stem
from tongdory2.fsx.mdmeta import read_md
from tongdory2.fsx.paths import real
from tongdory2.fsx.roots import RegisteredRoot, Role
from tongdory2.fsx.scan import ScanItem, scan_collect
from tongdory2.fsx.scope import Scope
from tongdory2.fsx.screenshots import batch_date, is_screenshot
from tongdory2.fsx.sensitivity import classify_sensitivity, mask, read_head

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "make_demo_data.py"
OLD = time.time() - 30 * 86400


# ---------------------------------------------------------------- helpers

def touch(p: Path, data: bytes | str = b"x", mtime: float = OLD) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode() if isinstance(data, str) else data)
    os.utime(p, (mtime, mtime))
    return p


def mkscope(home: Path, *roots: tuple[str, Path, Role]) -> tuple[list[RegisteredRoot], Scope]:
    regs = [RegisteredRoot(rid, real(p), role, confirmed=True) for rid, p, role in roots]
    app = home.parent / "appdata"
    app.mkdir(exist_ok=True)
    return regs, Scope(regs, home, real(app))


def item(path: Path, role: Role = Role.COLLECT, mtime: float = OLD, root: Path | None = None) -> ScanItem:
    st = os.lstat(path)
    rel = path.relative_to(root).as_posix() if root else path.name
    return ScanItem(path, "r", role, rel, path.is_dir() and not path.is_symlink(), st.st_size, mtime,
                    path.is_symlink())


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return real(h)


# ---------------------------------------------------------------- scan

def test_scan_collect_rules(home: Path):
    desk = home / "Desktop"
    touch(desk / "a.docx")
    touch(desk / "sub" / "deep" / "b.xlsx")
    touch(desk / "proj" / "package.json", "{}")
    touch(desk / "proj" / "index.js")
    touch(desk / "repo" / ".git" / "HEAD", "ref")
    touch(desk / "repo" / "README.md")
    touch(desk / "mk" / "Makefile")
    touch(desk / "mk" / "src" / "x.c")
    touch(desk / "node_modules" / "x" / "i.js")
    touch(desk / ".claude" / "settings.json")
    touch(home / "Pictures" / "secret.png")
    os.symlink(home / "Pictures", desk / "pics")  # 범위 밖 링크
    os.symlink(desk / "a.docx", desk / "a-link")  # 범위 안 링크
    nested = desk / "nested_root"
    touch(nested / "inner.txt")
    cur = home / "Documents" / "ws현재_2026"
    touch(cur / "02 doc" / "01.착수" / "plan.docx")
    touch(cur / "02 doc" / "04.설계" / "임시" / "draft.docx")
    touch(cur / "tmp" / "t.log")
    touch(cur / "tmp" / "inner" / "deep.txt")
    touch(cur / "01 source" / "tmp" / "no.txt")  # 소스 아래 엉망 폴더는 대상 아님
    touch(cur / "README.md")

    regs, scope = mkscope(home, ("D", desk, Role.COLLECT), ("N", nested, Role.BASELINE),
                          ("C", cur, Role.CURRENT))
    items = scan_collect(regs, scope)
    got = {(i.root_id, i.rel, i.is_dir, i.is_link) for i in items}
    assert got == {
        ("D", "a.docx", False, False),
        ("D", "sub/deep/b.xlsx", False, False),
        ("D", "proj", True, False),
        ("D", "repo", True, False),
        ("D", "mk", True, False),
        ("D", ".claude", True, False),
        ("D", "pics", False, True),
        ("D", "a-link", False, True),
        ("C", "02 doc/04.설계/임시/draft.docx", False, False),
        ("C", "tmp/t.log", False, False),
        ("C", "tmp/inner/deep.txt", False, False),
    }
    assert all("Pictures" not in str(i.path) for i in items)
    a = next(i for i in items if i.rel == "a.docx")
    assert a.size == 1 and a.mtime == pytest.approx(OLD) and a.role == Role.COLLECT


# ---------------------------------------------------------------- filters

def test_classify_reasons(home: Path):
    desk = home / "Desktop"
    files = {
        "ok.docx": None,
        ".DS_Store": "HIDDEN",
        ".hidden.txt": "HIDDEN",
        "~$문서.docx": "HIDDEN",
        "big.zip.crdownload": "TEMP_DOWNLOAD",
        "setup.dmg": "INSTALLER",
        "server.pem": "SENSITIVE",
        ".env.bak": "SENSITIVE",
        "CLAUDE.md": "AI_CONTEXT",
        "열린문서.docx": "OPEN_FILE",
    }
    for n in files:
        touch(desk / n)
    touch(desk / "~$린문서.docx")  # Word 잠금 파일 (앞 2자 잘림)
    touch(desk / "recent.docx", mtime=time.time() - 600)
    touch(desk / "repo" / ".git" / "HEAD")
    touch(desk / "proj" / "go.mod")
    touch(desk / "sdk.zip")
    (desk / "sdk").mkdir()
    touch(home / "Pictures" / "p.png")
    os.symlink(home / "Pictures" / "p.png", desk / "out-link")
    os.symlink(desk / "ok.docx", desk / "in-link")
    os.link(desk / "ok.docx", desk / "hard.docx")
    regs, scope = mkscope(home, ("D", desk, Role.COLLECT))
    now = time.time()
    by = {i.rel: classify(i, scope, now) for i in scan_collect(regs, scope)}
    for n, reason in files.items():
        if n != "ok.docx":
            assert by[n].excluded and by[n].reason == reason, (n, by[n])
    assert by["recent.docx"].reason == "RECENTLY_MODIFIED"
    assert classify(next(i for i in scan_collect(regs, scope) if i.rel == "recent.docx"), scope, now,
                    recent_hours=0.05).excluded is False
    assert by["repo"].reason == "GIT_REPO_FOLDER"
    assert by["proj"].reason == "PROJECT_FOLDER"
    assert by["sdk.zip"].reason == "INSTALLER"
    assert by["out-link"].reason == "OUT_OF_SCOPE" and "Pictures" not in by["out-link"].detail
    assert by["in-link"].reason == "EX_LINK"
    assert by["hard.docx"].reason == "EX_LINK"  # st_nlink > 1 (ok.docx 도 같은 inode)
    assert by[".env.bak"].detail.startswith("민감:")
    # 범위 밖 경로 직접 전달
    outside = item(touch(home / "Pictures" / "q.png"))
    assert classify(outside, scope, now).reason == "OUT_OF_SCOPE"


def test_ai_context_names():
    for rel in ("CLAUDE.md", "x/AGENTS.md", ".cursor/rules.md", ".archive/.cursor/a.md", ".github/prompts/p.md",
                ".github/copilot-instructions.md", ".aider.conf.yml", ".kiro/specs/a.md"):
        assert is_ai_context(rel), rel
    for rel in ("README.md", "docs/claude_notes.md", ".github/workflows/ci.yml"):
        assert not is_ai_context(rel), rel


# ---------------------------------------------------------------- sensitivity

@pytest.mark.parametrize("name,head,level", [
    ("server-prod.pem", None, "L3"),
    ("id_rsa", None, "L3"),
    ("id_ed25519.pub", None, "L3"),
    (".env", None, "L3"),
    (".env.production", None, "L3"),
    ("prod.kubeconfig", None, "L3"),
    ("credentials_gcp.json", None, "L3"),
    ("office.ovpn", None, "L3"),
    ("keystore.jks", None, "L3"),
    ("신분증사본.jpg", None, "L3"),
    ("주민등록등본.pdf", None, "L3"),
    ("내통장.png", None, "L3"),
    ("여권스캔.pdf", None, "L3"),
    ("운전면허증.jpg", None, "L3"),
    ("notes.txt", b"hello\n-----BEGIN OPENSSH PRIVATE KEY-----\nabc", "L3"),
    ("notes.txt", b"key=AKIAABCDEFGHIJKLMNOP", "L3"),
    ("conf.txt", b"DB_PASSWORD=hunter2\n", "L3"),
    ("list.csv", "김,900101-1234567\n이,850505-2345678".encode(), "L3"),
    ("계약서_한빛.pdf", None, "L2"),
    ("견적서_2차.pdf", None, "L2"),
    ("검토의견_1020.docx", None, "L2"),
    ("세금계산서_10월.pdf", None, "L2"),
    ("app.log", None, "L2"),
    ("image (3).png", None, "L2"),
    ("users.csv", b"a@b.com,010-1234-5678,c@d.org", "L2"),
    ("회의록.docx", None, "L1"),
    ("list.csv", "김,900101-1234567".encode(), "L1"),  # 주민번호 1건은 차단 기준(2건) 미만
    ("rfc8446.pdf", None, "L1"),
])
def test_classify_sensitivity(name: str, head: bytes | None, level: str):
    s = classify_sensitivity(Path(name), head)
    assert s.level == level, (name, s)
    assert s.signals or level == "L1"


def test_sensitivity_name_only_unlock():
    assert classify_sensitivity(Path("secret_plan.docx")).name_only
    assert not classify_sensitivity(Path("a.pem")).name_only


def test_mask():
    t = mask("메일 hong@example.com 전화 010-1234-5678 주민 900101-1234567 카드 4111 1111 1111 1111 "
             "계좌 1234567890123 금액 48,000,000원 날짜 2026-10-20")
    for s in ("hong@", "010-1234", "900101", "4111", "1234567890123", "48,000,000"):
        assert s not in t, s
    assert "2026-10-20" in t and t.count("***") == 6
    assert mask("") == ""


# ---------------------------------------------------------------- hashing

def test_sha256_and_dup_groups(tmp_path: Path):
    a = touch(tmp_path / "회의록.docx", b"same", mtime=OLD + 10)
    b = touch(tmp_path / "회의록(1).docx", b"same", mtime=OLD)  # 더 오래됐지만 (1) 토큰
    c = touch(tmp_path / "c.docx", b"same", mtime=OLD + 20)
    d = touch(tmp_path / "d.docx", b"diff")
    e = touch(tmp_path / "e.docx", b"other")
    assert sha256_file(a) == hashlib.sha256(b"same").hexdigest()
    os.symlink(a, tmp_path / "lnk")
    with pytest.raises(OSError):
        sha256_file(tmp_path / "lnk")
    items = [item(p) for p in (a, b, c, d, e)]
    for it, p in zip(items, (a, b, c, d, e)):
        it.mtime = os.lstat(p).st_mtime
    hashes = {p: sha256_file(p) for p in (a, b, c, d, e)}
    assert dup_groups(items, hashes) == {b: a, c: a}
    cur = tmp_path / "cur" / "e.docx"
    assert dup_groups(items, hashes, {hashes[e]: cur}) == {b: a, c: a, e: cur}


def test_binary_readers_keep_bytes_after_control_z(home, monkeypatch):
    desk = home / "Desktop"
    png_header = b"\x89PNG\r\n\x1a\n"
    first = touch(desk / "첫 이미지.png", png_header + b"abc")
    second = touch(desk / "둘째 이미지.png", png_header + b"xyz")
    document = touch(desk / "문서.md", b"# Title\r\n\x1aafter marker\r\n")
    _, scope = mkscope(home, ("D", desk, Role.COLLECT))
    document_bytes = document.read_bytes()

    open_fd = os.open
    native_flag = getattr(os, "O_BINARY", 0)
    flag = native_flag or (1 << 29)

    def checked_open(path, flags, *args, **kwargs):
        assert flags & flag
        return open_fd(path, flags if native_flag else flags & ~flag, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "O_BINARY", flag, raising=False)
        patch.setattr(os, "open", checked_open)
        hashes = {p: sha256_file(p, scope) for p in (first, second)}
        assert hashes[first] == hashlib.sha256(png_header + b"abc").hexdigest()
        assert hashes[second] == hashlib.sha256(png_header + b"xyz").hexdigest()
        assert read_head(document, scope) == document_bytes
        assert "after marker" in extract(document, "L1", scope=scope).text
        assert "after marker" in read_md(document).body
    assert not dup_groups([item(first), item(second)], hashes)


def test_version_groups(tmp_path: Path):
    names = {
        "요구사항정의서_v1.xlsx": 1, "요구사항정의서_v2.xlsx": 2, "요구사항정의서_final.xlsx": 3,
        "요구사항정의서_v3.xls": 4, "요구사항정의서.pdf": 5,
        "보고서_최종.docx": 1, "보고서_수정본_20261103.doc": 2, "보고서 - 복사본.docx": 3,
        "A_B_v0.8.docx": 1, "B_A_v0.9.docx": 2,
        "app-20261020.log": 1, "app-20261021.log": 2,
        "스크린샷 2026-11-03 오후 2.05.33.png": 1, "스크린샷 2026-11-03 오후 2.05.34.png": 2,
        "단독.docx": 1,
    }
    items = []
    for n, t in names.items():
        p = touch(tmp_path / n)
        it = item(p)
        it.mtime = OLD + t
        items.append(it)
    groups = {tuple(i.name for i in g) for g in version_groups(items)}
    assert groups == {
        ("요구사항정의서_v3.xls", "요구사항정의서_final.xlsx", "요구사항정의서_v2.xlsx", "요구사항정의서_v1.xlsx"),
        ("보고서 - 복사본.docx", "보고서_수정본_20261103.doc", "보고서_최종.docx"),
        ("B_A_v0.9.docx", "A_B_v0.8.docx"),
    }
    assert version_stem("견적서_QR결제_2차(1).pdf") == version_stem("견적서_QR결제_2차.pdf")


# ---------------------------------------------------------------- extract

def _make_office(tmp_path: Path) -> dict[str, Path]:
    from docx import Document
    from openpyxl import Workbook
    from pptx import Presentation
    out = {}
    d = Document()
    d.add_heading("인터페이스 설계서", 0)
    d.add_paragraph("담당 hong@example.com 010-1234-5678")
    d.add_paragraph("본문 " + "가" * 3000)
    out["docx"] = tmp_path / "설계.docx"
    d.save(out["docx"])
    wb = Workbook()
    ws = wb.active
    ws.title = "요구사항"
    ws.append(["요구사항ID", "내용"])
    for i in range(30):
        ws.append([f"REQ-{i:03d}", f"기능 {i}"])
    wb.create_sheet("변경이력")
    out["xlsx"] = tmp_path / "req.xlsx"
    wb.save(out["xlsx"])
    prs = Presentation()
    for t in ("화면정의서 v1", "화면 1: 결제"):
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = t
    out["pptx"] = tmp_path / "ui.pptx"
    prs.save(out["pptx"])
    body = "# 제목 줄\n둘째 줄 010-9999-8888\n" + "\n".join(f"l{i}" for i in range(100))
    out["text"] = touch(tmp_path / "note.md", body)
    out["log"] = touch(tmp_path / "server.log", "2026-10-20 ERROR first line\nmore")
    return out


def test_extract_levels(tmp_path: Path):
    f = _make_office(tmp_path)
    x = extract(f["docx"], "L1")
    assert x.kind == "docx" and x.title == "인터페이스 설계서"
    assert len(x.text) == 1500 and "hong@" not in x.text and "***" in x.text
    assert "담당 hong@example.com 010-1234-5678" in x.preview  # 로컬 뷰어용 원문
    x2 = extract(f["docx"], "L2")
    assert x2.text == "인터페이스 설계서" and x2.preview
    x3 = extract(f["docx"], "L3")
    assert x3.text == "" and x3.title == "" and x3.preview == [L3_NOTICE]

    xl = extract(f["xlsx"], "L1")
    assert xl.kind == "xlsx" and xl.preview[0] == "[시트] 요구사항"
    assert "요구사항ID | 내용" in xl.preview and any("REQ-013" in ln for ln in xl.preview)
    assert "REQ-005" not in xl.text and "REQ-004" in xl.text and "[시트] 변경이력" in xl.text
    pp = extract(f["pptx"], "L1")
    assert pp.preview == ["1. 화면정의서 v1", "2. 화면 1: 결제"] and pp.title == "화면정의서 v1"
    tx = extract(f["text"], "L1")
    assert tx.kind == "text" and tx.title == "제목 줄" and len(tx.preview) == 40 and "***" in tx.text
    lg = extract(f["log"], "L2")
    assert lg.text == "" and lg.title == "" and lg.preview[0].startswith("2026-10-20")
    assert len(extract(f["docx"], "L2").text) <= 80


def test_extract_never_raises(tmp_path: Path):
    for n in ("bad.docx", "bad.xlsx", "bad.pptx", "bad.pdf"):
        x = extract(touch(tmp_path / n, b"\x00garbage"), "L1")
        assert x.text == "" and x.preview == [] and x.kind == n.split(".")[1]
    z = tmp_path / "z.docx"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("x.txt", "not docx")
    assert extract(z, "L1").text == ""
    assert extract(tmp_path / "missing.pdf", "L1").kind == "pdf"
    assert extract(touch(tmp_path / "a.png", b"\x89PNG"), "L1").kind == "image"
    assert extract(touch(tmp_path / "a.bin", b"\x00"), "L1").kind == "binary"
    real_f = touch(tmp_path / "t.txt", "secret body")
    os.symlink(real_f, tmp_path / "t-link.txt")
    assert extract(tmp_path / "t-link.txt", "L1").text == ""  # 링크는 따라가지 않음


def test_extract_respects_scope(home: Path):
    desk = home / "Desktop"
    f = touch(desk / "a.txt", "hello")
    p = touch(home / "Pictures" / "b.txt", "nope")
    _, scope = mkscope(home, ("D", desk, Role.COLLECT))
    assert extract(f, "L1", scope=scope).text == "hello"
    assert extract(p, "L1", scope=scope).text == ""
    with pytest.raises(PermissionError):
        sha256_file(p, scope=scope)


# ---------------------------------------------------------------- screenshots

@pytest.mark.parametrize("name,day", [
    ("스크린샷 2026-11-03 오후 2.05.33.png", date(2026, 11, 3)),
    ("스크린샷 2026-11-03 오후 2.05.33.png", date(2026, 11, 3)),
    ("스크린샷 2026-11-03 오전 10.21.07.png", date(2026, 11, 3)),
    ("스크린샷 2026-11-03 오후 2.05.33 (2).png", date(2026, 11, 3)),
    ("Screenshot 2026-11-10 at 10.02.11.png", date(2026, 11, 10)),
    ("Screenshot 2026-11-10 at 10.02.11 AM.png", date(2026, 11, 10)),
    ("Screen Shot 2020-01-02 at 3.04.05 PM.png", date(2020, 1, 2)),
    ("KakaoTalk_20261103_183012.jpg", date(2026, 11, 3)),
    ("KakaoTalk_20261103_183012345_01.png", date(2026, 11, 3)),
    ("화면 캡처 2026-11-05 101530.png", date(2026, 11, 5)),
])
def test_is_screenshot(name: str, day: date):
    assert is_screenshot(name)
    assert batch_date(name, 0) == day


def test_not_screenshot_and_mtime_fallback():
    for n in ("image (3).png", "신분증사본.jpg", "스크린샷 메모.txt", "KakaoTalk_20261103_183012.pdf",
              "Screenshot 2026-11-10 at 10.02.11.pdf"):
        assert not is_screenshot(n), n
    t = time.mktime((2026, 10, 9, 18, 30, 0, 0, 0, -1))
    assert batch_date("image (3).png", t) == date(2026, 10, 9)


# ---------------------------------------------------------------- 데모 수락 테스트

SENS_MAP = {"NORMAL": "L1", "PUBLIC_REF": "L1", "REDUCED": "L2", "FILENAME_ONLY": "L2", "BLOCKED": "L3"}
COLLECT_IDS = ("DESKTOP", "DOWNLOADS", "KAKAO")


@pytest.fixture(scope="module")
def demo(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("fsx") / "demo"
    r = subprocess.run([sys.executable, str(SCRIPT), "--out", str(out), "--allow-outside-repo"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    return real(out)


@pytest.fixture(scope="module")
def run(demo: Path) -> dict:
    man = json.loads((demo / "_manifest.json").read_text(encoding="utf-8"))
    home = demo / "home"
    regs = [RegisteredRoot(r["id"], real(demo / r["rel_path"]), Role(r["role"]), confirmed=True)
            for r in man["roots"] if r["role"] in Role.__members__]
    app = demo.parent / "appdata"
    app.mkdir(exist_ok=True)
    scope = Scope(regs, home, real(app))
    now = time.time()
    items = scan_collect(regs, scope)
    by_rel = {it.path.relative_to(home).as_posix(): it for it in items}
    results = {rel: classify(it, scope, now) for rel, it in by_rel.items()}
    rows = {r["home_rel"]: r for r in man["items"]}
    return {"man": man, "home": home, "scope": scope, "now": now, "items": items, "by_rel": by_rel,
            "results": results, "rows": rows}


def _collect_rows(run: dict) -> list[dict]:
    return [r for r in run["man"]["items"] if r["root"] in COLLECT_IDS or r["group"] == "CUR"]


def test_demo_scan_matches_manifest(run: dict):
    man, by_rel = run["man"], run["by_rel"]
    expected = {r["home_rel"] for r in _collect_rows(run)}
    assert set(by_rel) == expected
    assert len([r for r in by_rel if r.split("/")[0] in ("Desktop", "Downloads") or "카카오톡" in r]) \
        == man["expected_counts"]["collect_total"]
    assert by_rel["Desktop/qr-sdk-sample"].is_dir and by_rel["Desktop/01 source 바로가기"].is_link
    assert not any(r.startswith("Desktop/qr-sdk-sample/") for r in by_rel)  # 내부 미탐색
    assert not any(r.startswith(("Pictures", "dev/", ".Trash")) for r in by_rel)


def test_demo_exclude_reasons(run: dict):
    res, now = run["results"], run["now"]
    for r in _collect_rows(run):
        got = res[r["home_rel"]]
        if r["expected_verdict"] == "EXCLUDE":
            accepted = {REASON_ALIASES.get(c, c) for c in r.get("accepted_reason_codes") or [r["reason_code"]]}
            assert got.excluded and got.reason in accepted, (r["no"], got)
        else:
            # 생성 시각에 가깝게 클램프된 파일은 실행 시각에 따라 '최근 수정'일 수 있다
            recent = run["by_rel"][r["home_rel"]].mtime > now - 2 * 3600
            assert not got.excluded or (recent and got.reason == "RECENTLY_MODIFIED"), (r["no"], got)
    n_ex = sum(1 for r in _collect_rows(run) if r["group"] == "SCATTER" and res[r["home_rel"]].excluded
               and res[r["home_rel"]].reason != "RECENTLY_MODIFIED")
    assert n_ex + 1 == run["man"]["expected_counts"]["scatter_verdicts_default"]["EXCLUDE"]


def test_demo_sensitivity(run: dict):
    for r in _collect_rows(run):
        if "sensitivity" not in r:
            continue
        s = run["results"][r["home_rel"]].sensitivity
        assert s is not None and s.level == SENS_MAP[r["sensitivity"]], (r["no"], r["sensitivity"], s)
    blocked = [r for r in _collect_rows(run) if r.get("sensitivity") == "BLOCKED"]
    assert len(blocked) == run["man"]["expected_counts"]["sensitive_blocked_scatter"]


def _passing(run: dict) -> list[ScanItem]:
    return [run["by_rel"][rel] for rel, fr in run["results"].items()
            if not fr.excluded and not run["by_rel"][rel].is_dir]


def test_demo_dup_pairs(run: dict):
    home, rows = run["home"], run["rows"]
    passing = _passing(run)
    hashes = {it.path: sha256_file(it.path, scope=run["scope"]) for it in passing}
    dups = dup_groups(passing, hashes)
    no = {it.path: str(rows[it.path.relative_to(home).as_posix()]["no"]) for it in passing}
    got = {(no[a], no[b]) for a, b in dups.items()}
    expected = {(str(r["no"]), str(r["dup_of"])) for r in _collect_rows(run) if r["expected_verdict"] == "DUP"}
    assert got == expected == {("30", "2")}
    for a, b in run["man"]["expected_counts"]["revision_pairs"]:  # 수정본은 중복 아님
        assert str(a) not in {x for x, _ in got} and str(b) not in {x for x, _ in got}


def test_demo_version_groups(run: dict):
    home, rows, man = run["home"], run["rows"], run["man"]
    passing = _passing(run)
    hashes = {it.path: sha256_file(it.path) for it in passing}
    dups = dup_groups(passing, hashes)
    pool = [it for it in passing if it.path not in dups]
    # 현재 과제 v0.8 (엉망 폴더 밖이라 스캔 대상 아님) — 호출자가 현재 과제 색인에서 넣어준다
    cur_root = next(r for r in man["roots"] if r["id"] == "CUR")
    v08 = next(f for f in man["files"] if f["home_rel"].startswith(cur_root["home_rel"]) and "v0.8" in f["home_rel"])
    p = home / v08["home_rel"]
    pool.append(ScanItem(p, "CUR", Role.CURRENT, v08["home_rel"], False, p.stat().st_size, p.stat().st_mtime, False))

    def label(it: ScanItem) -> str:
        rel = it.path.relative_to(home).as_posix()
        return "CUR v0.8" if rel == v08["home_rel"] else str(rows[rel]["no"])

    got = [[label(it) for it in g] for g in version_groups(pool)]
    expected = man["expected_counts"]["version_groups"]
    for vg in expected.values():
        members = {str(m) for m in vg["members"]}
        match = [g for g in got if set(g) == members]
        assert match, (vg, got)
        assert match[0][0] == str(vg["latest"])
    # 그 밖의 묶음은 R-6.6 수정본 쌍뿐
    extra = [set(g) for g in got if set(g) not in [{str(m) for m in vg["members"]} for vg in expected.values()]]
    assert extra == [{str(a), str(b)} for a, b in man["expected_counts"]["revision_pairs"]
                     if str(a) in rows_no(run)]


def rows_no(run: dict) -> set[str]:
    return {str(r["no"]) for r in _collect_rows(run)}


def test_demo_screenshots(run: dict):
    man, by_rel = run["man"], run["by_rel"]
    batches: Counter[str] = Counter()
    for rel, it in by_rel.items():
        row = run["rows"][rel]
        assert is_screenshot(it.name) == bool(row.get("is_screenshot")), rel
        if is_screenshot(it.name):
            d = batch_date(it.name, it.mtime).isoformat()
            assert d == row["screenshot_batch"], rel
            batches[d] += 1
    assert dict(batches) == man["expected_counts"]["screenshot_batches"]
    assert sum(batches.values()) == man["expected_counts"]["screenshots_total"]


def test_demo_preview_expect(demo: Path, run: dict):
    seen = set()
    for r in run["man"]["items"]:
        pe = r.get("preview_expect")
        if not pe:
            continue
        p = demo / r["rel_path"]
        level = SENS_MAP[r.get("sensitivity", "NORMAL")]
        x = extract(p, level, scope=run["scope"])
        joined = "\n".join(x.preview)
        for snip in pe["snippets"]:
            assert snip in joined, (r["no"], snip, x.preview[:10])
        seen.add(x.kind)
    assert {"pdf", "docx", "xlsx", "pptx"} <= seen


def test_demo_l3_never_read(run: dict):
    for r in _collect_rows(run):
        if r.get("sensitivity") == "BLOCKED":
            x = extract(run["home"] / r["home_rel"], "L3")
            assert x.text == "" and x.preview == [L3_NOTICE]


def test_scan_reports_fraction_by_top_level_entries(tmp_path):
    """파일 종류 살펴보기 진행률: 전체 파일 수를 몰라도 맨 위 항목을 지난 비율로 0→1까지 오른다."""
    home = tmp_path / "home"
    a, b = home / "Desktop", home / "Downloads"
    for name in ("x", "y", "z"):
        touch(a / name / "f.txt")
    touch(b / "only.txt")
    regs, scope = mkscope(home, ("A", a, Role.COLLECT), ("B", b, Role.COLLECT))
    seen: list[float] = []
    scan_collect(regs, scope, on_fraction=seen.append)
    assert seen == sorted(seen) and seen[0] == 0 and seen[-1] == 1
    assert any(0 < f < 0.5 for f in seen)  # 첫 폴더 안에서도 오른다

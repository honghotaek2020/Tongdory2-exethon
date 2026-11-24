"""민감 등급 판정과 마스킹 (02_정책 §3.2·§3.3, 결정 #4·#13).

등급: L1=NORMAL(마스킹 본문 1,500자) · L2=REDUCED(마스킹 파일명 + 제목 ≤80자, 로그·이미지는 파일명만)
· L3=BLOCKED(아무것도 보내지 않음, 파일명 포함).

이 모듈은 파일을 직접 열지 않는다(`classify_sensitivity`는 이름 + 호출자가 넘긴 앞부분만 본다).
앞부분 읽기는 `read_head()`가 등록 범위 검사(`Scope.check(READ)`)를 통과한 뒤 링크 미추적으로 한다.
"""

from __future__ import annotations

import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tongdory2.fsx.scope import Scope

HEAD_BYTES = 4096

# ---------------- L3 차단 ----------------
L3_EXTS = {
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".crt", ".cer", ".der", ".ppk", ".ovpn",
    ".kdbx", ".gpg", ".tfstate", ".htpasswd", ".kubeconfig",
}
# (regex, signal) — 이름 전체(소문자, NFC)에 대해 search
_L3_NAME: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\.env(\..*)?$|.*\.env(\..*)?$|env\.(bak|old|prod|local)"), "ENV_FILE"),
    (re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)(_.*)?(\.pub)?$"), "SSH_KEY_NAME"),
    (re.compile(r"^kubeconfig"), "SECRET_NAME"),
    (re.compile(r"^(\.npmrc|\.pypirc|\.netrc|\.git-credentials)$"), "SECRET_NAME"),
    (re.compile(r"credentials?|secret|passw(or)?d|pwd|token|api[_-]?key"), "SECRET_NAME"),
]
_L3_KO_NAME = re.compile(
    r"신분증|주민(등록)?|운전면허|여권|통장|계좌|가족관계|등본|초본|급여명세|원천징수|연봉|인사평가|비밀번호"
)
_L3_MARKERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"), "KEY_HEADER"),
    (re.compile(r"BEGIN PGP PRIVATE KEY BLOCK"), "KEY_HEADER"),
    (re.compile(r"-----BEGIN CERTIFICATE-----"), "CERT_HEADER"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "CLOUD_KEY"),
    (re.compile(r"(?i)aws_secret_access_key\s*[=:]"), "CLOUD_KEY"),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}"), "TOKEN"),
    (re.compile(r"\bxox[bp]-"), "TOKEN"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "TOKEN"),
    (re.compile(r"(?m)^[A-Z0-9_]*(PASSWORD|SECRET|TOKEN|API_KEY|PRIVATE_KEY)\s*="), "SECRET_ASSIGN"),
]
_RRN = re.compile(r"(?<!\d)\d{6}-?[1-4]\d{6}(?!\d)")

# ---------------- L2 요약만 ----------------
_L2_NAME = re.compile(r"계약서|견적서|발주서|세금계산서|합의서|협약|nda|비밀유지|날인|검토의견")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".heic", ".heif", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+82[-\s]?)?0\d{1,2}[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)")
_CARD = re.compile(r"(?<!\d)\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}(?!\d)")

# ---------------- L1 참고(규격서) 신호 ----------------
_PUBLIC_REF = re.compile(r"^(rfc\d+|sgp\.\d+|iso[-_ ]?\d+|ieee|w3c)|_spec_|specification")


@dataclass
class Sensitivity:
    level: str  # "L1" | "L2" | "L3"
    signals: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """03 명세 명칭 (결정 #13)."""
        return {"L1": "NORMAL", "L2": "REDUCED", "L3": "BLOCKED"}[self.level]

    @property
    def name_only(self) -> bool:
        """파일명 패턴으로만 걸렸는가 — R-3.2 행 단위 '민감 아님' 해제 가능 여부."""
        return bool(self.signals) and all(s in NAME_SIGNALS for s in self.signals)


NAME_SIGNALS = {"SECRET_NAME", "ID_CARD_NAME", "CONTRACT_NAME"}
KIND_LABEL = {
    "KEY_EXT": "개인키·인증서", "KEY_HEADER": "개인키", "CERT_HEADER": "인증서", "SSH_KEY_NAME": "SSH 키",
    "ENV_FILE": "환경변수 파일", "SECRET_NAME": "비밀값 파일", "CLOUD_KEY": "클라우드 키", "TOKEN": "토큰",
    "SECRET_ASSIGN": "비밀값", "ID_CARD_NAME": "신분·개인정보 문서", "PII_RRN": "주민등록번호",
}


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _decode(head: bytes) -> str:
    return head.decode("utf-8", errors="ignore")


def classify_sensitivity(path: Path, head: bytes | None = None) -> Sensitivity:
    """확장자·파일명·앞부분(head, 보통 앞 4KB)으로 등급을 정한다. 파일을 직접 읽지 않는다."""
    name = _nfc(Path(path).name)
    low = name.lower()
    ext = Path(low).suffix
    l3: list[str] = []

    if ext in L3_EXTS:
        l3.append("KEY_EXT")
    for rx, sig in _L3_NAME:
        if rx.search(low) and sig not in l3:
            l3.append(sig)
    if _L3_KO_NAME.search(name):
        l3.append("ID_CARD_NAME")

    text = _decode(head) if head else ""
    if text:
        for rx, sig in _L3_MARKERS:
            if rx.search(text) and sig not in l3:
                l3.append(sig)
        if len(_RRN.findall(text)) >= 2:
            l3.append("PII_RRN")
    if l3:
        return Sensitivity("L3", l3)

    l2: list[str] = []
    if _L2_NAME.search(low):
        l2.append("CONTRACT_NAME")
    if ext == ".log" or (ext == ".txt" and "log" in low):
        l2.append("LOG_FILE")
    if ext in IMAGE_EXTS:
        l2.append("IMAGE")
    if text:
        cards = [c for c in _CARD.findall(text) if _luhn(re.sub(r"\D", "", c))]
        if len(_EMAIL.findall(text)) + len(_PHONE.findall(text)) + len(cards) >= 3:
            l2.append("PII_DENSE")
    if l2:
        return Sensitivity("L2", l2)

    l1: list[str] = []
    if _PUBLIC_REF.search(low):
        l1.append("SPEC_DOC")
    return Sensitivity("L1", l1)


# ---------------- 마스킹 ----------------
_AMOUNT = re.compile(r"(?<![\d,])\d{1,3}(?:,\d{3}){2,}(?:\s?원)?|\d+\s?(?:만|억)\s?원")
_LONG_DIGITS = re.compile(r"(?<!\d)\d{10,}(?!\d)")


def mask(text: str) -> str:
    """이메일·전화번호·주민번호·카드번호·긴 숫자열·금액 → `***`."""
    if not text:
        return text
    out = _EMAIL.sub("***", text)
    out = _RRN.sub("***", out)
    out = _CARD.sub("***", out)
    out = _PHONE.sub("***", out)
    out = _AMOUNT.sub("***", out)
    out = _LONG_DIGITS.sub("***", out)
    return out


def read_head(path: Path, scope: "Scope", n: int = HEAD_BYTES) -> bytes | None:
    """등록 범위 검사 통과 + 일반 파일 + 링크 미추적(O_NOFOLLOW)일 때만 앞 n바이트를 읽는다."""
    from tongdory2.fsx.scope import Access

    if not scope.check(path, Access.READ).allowed:
        return None
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        try:
            return os.read(fd, n)
        finally:
            os.close(fd)
    except OSError:
        return None

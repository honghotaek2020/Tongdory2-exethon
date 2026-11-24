"""스크린샷 탐지와 날짜별 묶음 (결정 #40, TC M-07).

이름만 본다(이미지 바이트는 읽지 않음). macOS는 오전/오후·AM/PM 앞 공백이 U+202F일 수 있다.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

_SP = r"[    ]"
_IMG = r"\.(?:png|jpe?g|heic|gif|bmp|webp|tiff?)$"
_DATE = r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})"
_SUFFIX = rf"(?:{_SP}*\(\d+\)|{_SP}+\d+)?"  # 같은 초에 찍힌 "… (2).png" / "… 2.png"

_PATTERNS = [
    # 스크린샷 2026-11-03 오후 2.05.33.png / 스크린샷 2026-11-03 14.05.33.png
    re.compile(rf"^(?:스크린샷|스크린숏){_SP}+{_DATE}(?:{_SP}+|{_SP}*(?:at){_SP}+)?"
               rf"(?:(?:오전|오후){_SP}*)?\d{{1,2}}[.:]\d{{2}}[.:]\d{{2}}{_SUFFIX}{_IMG}", re.I),
    # Screenshot 2026-11-10 at 10.02.11.png / Screen Shot 2026-11-10 at 10.02.11 AM.png
    re.compile(rf"^Screen{_SP}?shot{_SP}+{_DATE}{_SP}+at{_SP}+\d{{1,2}}[.:]\d{{2}}[.:]\d{{2}}"
               rf"(?:{_SP}*[AP]M)?{_SUFFIX}{_IMG}", re.I),
    # Screenshot_20261110-100211.png (Android/기타)
    re.compile(rf"^Screenshot[_\-]?(?P<y>\d{{4}})(?P<m>\d{{2}})(?P<d>\d{{2}})[_\-]\d{{4,6}}.*{_IMG}", re.I),
    # KakaoTalk_20261103_183012.jpg / KakaoTalk_20261103_183012123.png / KakaoTalk_Photo_2026-11-03-18-30-12.jpeg
    re.compile(r"^KakaoTalk_(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})_\d{6,9}.*\.(?:jpe?g|png)$", re.I),
    re.compile(rf"^KakaoTalk_Photo_{_DATE}-\d{{2}}-\d{{2}}-\d{{2}}.*\.(?:jpe?g|png)$", re.I),
    # 화면 캡처 2026-11-03 101530.png (Windows 캡처 도구)
    re.compile(rf"^화면{_SP}?캡처{_SP}+{_DATE}.*{_IMG}", re.I),
]


def _match(name: str) -> re.Match[str] | None:
    n = unicodedata.normalize("NFC", name)
    for rx in _PATTERNS:
        m = rx.match(n)
        if m:
            return m
    return None


def is_screenshot(name: str) -> bool:
    return _match(name) is not None


def batch_date(name: str, mtime: float) -> date:
    """묶음 날짜: 파일명 날짜 우선, 없거나 잘못되면 mtime(로컬 시간대) 날짜."""
    m = _match(name)
    if m:
        try:
            return date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
        except (ValueError, IndexError):
            pass
    return datetime.fromtimestamp(mtime).date()

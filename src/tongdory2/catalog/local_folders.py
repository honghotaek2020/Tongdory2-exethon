"""확정한 폴더 구조와 원본 파일명·상위 폴더 이름을 로컬에서 대조한다 (외부 전송 없음).

폴더마다 비교어를 만든다.
  - 폴더 이름 전체·별칭 (가중치 3)
  - 문서 종류 꼬리말(결과서·설계서·시나리오 …) (2), 꼬리말에서 '서'를 뗀 말(결과·설계) (1.5)
  - 꼬리말 앞 수식어(화면·테스트·요구사항 …) (1)
  - 설명문 ':' 뒤에 나열한 문서 이름 (2)
  - 위 말의 한영 동의어(design↔설계, test↔테스트 …) — 원래 말과 같은 가중치
대조 순서: 파일명 → 가까운 상위 폴더 → 먼 상위 폴더. 처음으로 맞는 말이 있는 단계에서 정한다.
같은 점수로 여러 폴더가 맞으면 그 폴더들의 공통 상위 폴더로, 공통 상위가 없으면 '여러 폴더 일치'.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePath

from tongdory2.spec.normalize import clean

W_FULL, W_HEAD, W_STEM, W_QUAL, W_DESC = 3.0, 2.0, 1.5, 1.0, 2.0

# 문서 종류를 나타내는 꼬리말 (긴 것부터 대조)
HEADS = sorted(("정의서", "설계서", "결과서", "계획서", "보고서", "명세서", "설명서", "시나리오", "매뉴얼", "메뉴얼",
                "가이드", "회의록", "보고", "현황", "분석", "목록", "산출물", "체크리스트"), key=len, reverse=True)

# 한영 동의어 — 키가 폴더 비교어에 들어 있으면 값들도 같은 가중치로 비교어에 넣는다.
SYNONYMS = {
    "설계": ("design", "아키텍처", "아키텍쳐", "architecture", "구성도"), "테스트": ("test", "tests", "테스트", "시험"),
    "시나리오": ("scenario", "testcase", "testcases", "tc"), "결과": ("result", "results"),
    "보고": ("report", "reports"), "매뉴얼": ("manual", "메뉴얼", "설명서"), "메뉴얼": ("manual", "매뉴얼", "설명서"),
    "가이드": ("guide", "guideline"), "회의록": ("minutes", "meeting", "회의"), "요구사항": ("requirement", "requirements", "srs"),
    "계획": ("plan",), "이행": ("migration", "deploy", "deployment", "cutover"), "운영": ("operation", "ops"),
    "주간": ("weekly",), "착수": ("kickoff",), "인터페이스": ("interface", "api"), "화면": ("screen", "ui"),
    "분석": ("analysis",), "현행": ("asis",), "개발": ("dev", "development"), "구현": ("implementation", "impl"),
    "체크리스트": ("checklist",), "명세": ("spec", "specification"),
    "화면설계서": ("화면정의서", "스토리보드", "storyboard", "wireframe"),
}

# 양식·템플릿은 내용과 상관없이 이름에 문서 종류가 들어가므로 대조하지 않는다.
SKIP_WORDS = {"양식", "서식", "템플릿", "샘플", "참고용"}
STEM_HEADS = {"결과서", "설계서"}  # '서'를 뗀 말도 그 문서 종류로 볼 만큼 뚜렷한 꼬리말만 (정의·설명은 너무 넓음)
MIN_DECIDE = 1.5  # 수식어 하나(화면·테스트)만 맞으면 그 단계에서 정하지 않고 상위 폴더를 더 본다
_DESC_STOP = {"자료", "폴더", "문서", "영역", "단계", "기록", "그", "버전들"}
_LATIN = re.compile(r"[a-z0-9]+")
_HANGUL = re.compile(r"[가-힣]")


def _compact(value: str) -> str:
    return re.sub(r"[\W_]+", "", clean(value).casefold())


def _latin_words(value: str) -> set[str]:
    return set(_LATIN.findall(clean(value).casefold()))


@dataclass
class Match:
    node: dict | None  # 정한 폴더 (여러 폴더가 같게 맞고 공통 상위가 없으면 None)
    basis: str  # "file" 파일명 | "folder" 상위 폴더 이름
    score: float
    terms: list[str] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)  # 여러 폴더 일치일 때 후보
    folder: str = ""  # basis == "folder"일 때 근거가 된 상위 폴더 이름


class LocalFolderRules:
    def __init__(self, nodes: list[dict]) -> None:
        adopted = [n for n in nodes if n["status"] == "ADOPTED"]
        # 상위 폴더도 목적지가 될 수 있다 (예: 같은 '설계서'가 두 하위 폴더에 맞으면 '03.설계'로).
        self.nodes = {n["node_id"]: n for n in adopted if n["role"] == "DEST" and n["profile"] in ("common", "build")}
        self._by_path = {n["display_path"]: n for n in self.nodes.values()}
        parents = {n.get("parent_id") for n in adopted}
        self.terms: dict[str, dict[str, float]] = {}  # 비교어 → {node_id: 가중치}
        labels = {_compact(n["label"]) for n in self.nodes.values()}
        for node_id, node in self.nodes.items():
            for term, weight in _node_terms(node, node_id in parents, labels).items():
                slot = self.terms.setdefault(term, {})
                slot[node_id] = max(weight, slot.get(node_id, 0.0))
        self._hangul = sorted((t for t in self.terms if _HANGUL.search(t)), key=len, reverse=True)
        self._latin = {t for t in self.terms if not _HANGUL.search(t)}

    # ------------------------------------------------------------------ 대조
    def _score(self, segment: str) -> tuple[dict[str, float], list[str]]:
        text = _compact(segment)
        spans: list[tuple[int, int, str]] = []
        for term in self._hangul:  # 긴 말부터: 이미 맞은 긴 말 안의 짧은 말(설계서 안의 설계)은 세지 않는다
            for m in re.finditer(re.escape(term), text):
                s, e = m.span()
                if not any(a <= s and e <= b for a, b, _ in spans):
                    spans.append((s, e, term))
        hit_terms = [t for _, _, t in spans]
        words = _latin_words(segment)
        hit_terms += sorted(words & self._latin)
        scores: dict[str, float] = {}
        for term in dict.fromkeys(hit_terms):
            for node_id, weight in self.terms[term].items():
                scores[node_id] = scores.get(node_id, 0.0) + weight
        return scores, list(dict.fromkeys(hit_terms))

    def _decide(self, scores: dict[str, float]) -> tuple[dict | None, float, list[dict]]:
        best = max(scores.values())
        top = [self.nodes[i] for i in sorted(scores) if scores[i] == best]
        if len(top) == 1:
            return top[0], best, []
        # 맞은 폴더 중 다른 폴더의 상위가 있으면(예: '보고'와 '보고/주간보고') 하위를 고른다 → 아니면 공통 상위
        paths = [n["display_path"] for n in top]
        leaves = [n for n in top if not any(p != n["display_path"] and p.startswith(n["display_path"] + "/") for p in paths)]
        if len(leaves) == 1:
            return leaves[0], best, []
        parts = [PurePath(n["display_path"]).parts for n in leaves]
        common = []
        for level in zip(*parts):
            if len(set(level)) != 1:
                break
            common.append(level[0])
        while common:
            parent = self._by_path.get("/".join(common))
            if parent:
                return parent, best, leaves
            common.pop()
        return None, best, leaves

    def match(self, relative_path: str) -> Match | None:
        path = PurePath(relative_path)
        segments = [("folder", p) for p in reversed(path.parent.parts)]
        if not any(w in _compact(path.stem) for w in SKIP_WORDS):  # 양식·템플릿 파일은 파일명으로 정하지 않는다
            segments.insert(0, ("file", path.stem))
        weak = None
        for basis, segment in segments:
            scores, terms = self._score(segment)
            if not scores:
                continue
            node, score, cands = self._decide(scores)
            found = Match(node, basis, score, terms, cands, segment if basis == "folder" else "")
            if score >= MIN_DECIDE:
                return found
            weak = weak or found  # 약한 근거는 더 위 폴더에서도 못 정할 때만 쓴다
        return weak

    def candidates(self, relative_path: str) -> list[dict]:
        """이전 호출부 호환: 정한 폴더 하나 또는 여러 폴더 일치 후보."""
        m = self.match(relative_path)
        if not m:
            return []
        return [m.node] if m.node else m.candidates


def _split_label(label: str) -> tuple[str | None, str]:
    """'테스트결과서' → ('결과서', '테스트'), '설계' → (None, '설계')."""
    for head in HEADS:
        if label.endswith(head) and len(label) > len(head):
            return head, label[: -len(head)]
    return None, label


def _add(terms: dict[str, float], term: str, weight: float) -> None:
    term = _compact(term)
    if len(term) < 2:
        return
    for word in (term, *(_compact(a) for a in SYNONYMS.get(term, ()))):
        if len(word) >= 2:
            terms[word] = max(weight, terms.get(word, 0.0))


def _node_terms(node: dict, is_parent: bool = False, labels: set[str] = frozenset()) -> dict[str, float]:
    """is_parent: 하위 폴더가 있는 상위 폴더는 이름이 넓은 분류라 하위 폴더보다 낮게 친다."""
    terms: dict[str, float] = {}
    names = [node["label"]] + [re.sub(r"^\s*\d{1,3}\s*[.\-_)]?\s*", "", a) for a in node.get("aliases") or []]
    for name in names:
        label = _compact(name)
        if not label:
            continue
        _add(terms, label, W_STEM if is_parent else W_FULL)
        head, qual = _split_label(label)
        # 꼬리말이 다른 폴더 이름이면(착수보고서의 '보고서' ↔ '08.보고') 그 폴더 몫으로 두고 여기서는 쓰지 않는다
        if head and not ({head, head[:-1] if head.endswith("서") else head} & (labels - {label})):
            _add(terms, head, W_HEAD)
            if head in STEM_HEADS:
                _add(terms, head[:-1], W_STEM)
            if len(qual) >= 2:
                _add(terms, qual, W_QUAL)
        # 이름 안의 알려진 말(화면설계서 → 화면·설계)도 비교어로 — 동의어(screen·design)가 함께 들어간다
        for key in SYNONYMS:
            if key in label and key != label and key not in labels:
                _add(terms, key, W_QUAL if key in qual else W_STEM)
    desc = node.get("description") or ""
    if ":" in desc:
        for raw in re.split(r"[,，、·/]", desc.split(":", 1)[1]):
            term = _compact(raw)
            if 2 <= len(term) <= 12 and term not in _DESC_STOP:
                _add(terms, term, W_DESC)
    return terms

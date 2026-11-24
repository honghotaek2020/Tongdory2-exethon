"""Liner API 응답 형식을 직접 확인하는 개발용 스크립트.

실행: LINER_API_KEY=<키> uv run python scripts/probe_liner.py
키는 환경 변수 LINER_API_KEY에서만 읽는다(.env 파일은 읽지 않음). 결과는 표준출력(JSON)으로만 낸다.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import httpx

KEY = os.getenv("LINER_API_KEY", "").strip()
if not KEY:
    raise SystemExit("[Liner 진단] 환경 변수 또는 .env에 LINER_API_KEY를 설정해 주세요.")
BASE = os.environ.get("LINER_BASE_URL", "https://platform.liner.com/api/v1").rstrip("/")
BEARER = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
XKEY = {"x-api-key": KEY, "Content-Type": "application/json"}

client = httpx.Client(timeout=60)
report: dict = {}


def call(name: str, method: str, path: str, headers: dict, body: dict | None = None) -> dict | None:
    t0 = time.time()
    try:
        r = client.request(method, BASE + path, headers=headers, json=body)
    except httpx.HTTPError as exc:
        report[name] = {"error": f"network: {exc}"}
        return None
    info = {
        "status": r.status_code,
        "ms": int((time.time() - t0) * 1000),
        "x-request-id": r.headers.get("x-request-id"),
        "x-liner-ignored-parameters": r.headers.get("x-liner-ignored-parameters"),
    }
    try:
        data = r.json()
    except json.JSONDecodeError:
        data = {"raw": r.text[:300]}
    if r.status_code >= 400:
        info["error_body"] = data
    report[name] = info
    return data if r.status_code < 400 else None


def cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


# 1) Credits balance — auth header & fields
for hdr_name, hdr in (("bearer", BEARER), ("x-api-key", XKEY)):
    d = call(f"credits_{hdr_name}", "GET", "/credits/balance", hdr)
    if d is not None:
        report[f"credits_{hdr_name}"]["fields"] = d

# 2) Chat completions — strict json_schema with pattern / maxLength / nullable enum
schema = {
    "name": "node_judgment",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "decision": {"type": "string", "enum": ["NODE", "NEW_NODE", "HOLD"]},
            "node_id": {"type": ["string", "null"], "pattern": "^doc\\.[0-9]{2}(\\.[0-9]{2})?$"},
            "reason": {"type": "string", "maxLength": 60},
            "hold_reason": {"anyOf": [{"type": "string", "enum": ["LOW_SCORE", "CONFLICT"]}, {"type": "null"}]},
        },
        "required": ["decision", "node_id", "reason", "hold_reason"],
    },
}
d = call(
    "chat_json_schema",
    "POST",
    "/chat/completions",
    BEARER,
    {
        "model": "liner-gpt-router",
        "routing_strategy": "cost",
        "messages": [
            {"role": "system", "content": "파일을 후보 노드 중 하나로 분류하라. 근거는 한국어 한 줄."},
            {
                "role": "user",
                "content": "파일: 1120_주간회의록.docx (본문: 주간회의, 요구사항 3건 변경)\n"
                "후보: doc.08.01 회의록 / doc.03.01 요구사항정의서 / doc.04.03 인터페이스설계서",
            },
        ],
        "response_format": {"type": "json_schema", "json_schema": schema},
        "max_completion_tokens": 300,
    },
)
if d:
    report["chat_json_schema"].update(
        model=d.get("model"),
        usage=d.get("usage"),
        content=d["choices"][0]["message"].get("content"),
    )

# 3) Embeddings — bge-m3 Korean sanity + cost + dims
texts = [
    "회의록: 주간회의·킥오프 등 회의 결과 기록",
    "1120_주간회의록.docx 주간회의, 요구사항 3건 변경",
    "인터페이스설계서: 대외 전문 레이아웃과 API 명세",
    "한빛카드_QR결제_전문레이아웃_v1.2.xlsx 승인/취소 전문 필드 정의",
]
for model in ("baai/bge-m3", "openai/text-embedding-3-small"):
    d = call(f"emb_{model}", "POST", "/embeddings", BEARER, {"model": model, "input": texts})
    if d:
        v = [x["embedding"] for x in d["data"]]
        report[f"emb_{model}"].update(
            dims=len(v[0]),
            usage=d.get("usage"),
            sim_meeting_correct=round(cos(v[0], v[1]), 3),
            sim_meeting_wrong=round(cos(v[2], v[1]), 3),
            sim_iface_correct=round(cos(v[2], v[3]), 3),
            sim_iface_wrong=round(cos(v[0], v[3]), 3),
        )

# 4) Rerank — Korean
docs = [
    "08.보고/01.회의록: 주간회의·킥오프 등 회의 결과 기록",
    "03.분석/01.요구사항정의서: 기능·비기능 요구사항 정의 문서",
    "04.설계/03.인터페이스설계서: 대외 전문 레이아웃과 API 명세",
    "09.참고/01.표준규격: RFC·ISO·GSMA 등 외부 표준 규격 사본",
]
for model in ("cohere/rerank-5-fast", "qwen/qwen3-reranker-8b"):
    d = call(
        f"rerank_{model}",
        "POST",
        "/rerank",
        BEARER,
        {"model": model, "query": "rfc8446.pdf TLS 1.3 프로토콜 규격 요약 사본", "documents": docs, "top_n": 3},
    )
    if d:
        report[f"rerank_{model}"].update(results=d.get("results"), usage=d.get("usage"), meta=d.get("meta"))

# 5) Decisions — Korean choice
d = call(
    "decisions_jev-1.14",
    "POST",
    "/decisions",
    BEARER,
    {
        "model": "typesafe/jev-1.14",
        "state": {"file": "장애보고서_1118.docx", "excerpt": "누리은행 이체 모듈 장애 경위와 조치"},
        "questions": {
            "project": {
                "type": "choice",
                "instructions": "이 파일은 어느 과제의 자료인가?",
                "criteria": {
                    "hanbit": "한빛카드 QR결제 과제",
                    "mirae": "누리은행 대외계 유지보수 과제",
                    "none": "어느 과제와도 무관",
                },
            }
        },
    },
)
if d:
    report["decisions_jev-1.14"].update(answers=d.get("answers"), usage=d.get("usage"))

print(json.dumps(report, ensure_ascii=False, indent=1))

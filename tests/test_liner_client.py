"""LinerClient 단위 테스트 — 네트워크 없음 (httpx.MockTransport)."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from tongdory2.liner.client import (
    DECISION_MODEL,
    EMBED_MODEL,
    RERANK_MODEL,
    LinerClient,
    LinerError,
)
from tongdory2.liner.pricing import FALLBACK_PRICE, estimate_chat_cost

BASE = "https://platform.liner.com/api/v1"
KEY = "sk-test-SECRET-123"


class Recorder:
    """요청을 기록하고 큐에 쌓인 응답(또는 핸들러)을 순서대로 돌려준다."""

    def __init__(self, *responses):
        self.requests: list[httpx.Request] = []
        self.responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return r(request) if callable(r) else r

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)


def ok(data: dict, rid: str = "req_1", **headers) -> httpx.Response:
    return httpx.Response(200, json=data, headers={"x-request-id": rid, **headers})


def make(rec: Recorder, conn=None, **kw) -> LinerClient:
    c = LinerClient(KEY, BASE, conn=conn, run_id="run_1", transport=httpx.MockTransport(rec), **kw)
    c.sleeps = []

    async def fake_sleep(s):
        c.sleeps.append(s)

    c._sleep = fake_sleep
    return c


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    yield c
    c.close()


def rows(conn):
    return conn.execute("SELECT endpoint, model, request_id, cost_usd, cost_kind, run_id FROM api_calls"
                        " ORDER BY rowid").fetchall()


CHAT_OK = {
    "model": "gpt-6-luna-2026-10-01",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": '{"decision":"NODE","node_id":"doc.08.01"}'}}],
    "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "prompt_tokens_details": {"cached_tokens": 200}},
}


# ---------------------------------------------------------------- request shapes
async def test_embed_shape_and_empty_alignment(conn):
    def handler(req):
        inp = json.loads(req.content)["input"]
        return ok({"data": [{"index": i, "embedding": [float(i + 1), 0.5]} for i in range(len(inp))][::-1],
                   "usage": {"cost": 1e-7}})

    rec = Recorder(handler)
    async with make(rec, conn) as c:
        vecs = await c.embed(["가", "", "나", "   "])
    req = rec.requests[0]
    assert req.url == httpx.URL(BASE + "/embeddings")
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert "x-api-key" not in req.headers
    assert rec.body() == {"model": EMBED_MODEL, "input": ["가", "나"]}
    assert vecs == [[1.0, 0.5], [0.0, 0.0], [2.0, 0.5], [0.0, 0.0]]
    assert rows(conn) == [("/embeddings", EMBED_MODEL, "req_1", 1e-7, "REPORTED", "run_1")]


async def test_embed_batches_of_256():
    def handler(req):
        inp = json.loads(req.content)["input"]
        return ok({"data": [{"index": i, "embedding": [1.0]} for i in range(len(inp))], "usage": {"cost": 0}})

    rec = Recorder(handler)
    async with make(rec) as c:
        vecs = await c.embed([f"t{i}" for i in range(600)])
    assert sorted(len(json.loads(r.content)["input"]) for r in rec.requests) == [88, 256, 256]
    assert len(vecs) == 600


async def test_embed_all_empty_no_call():
    rec = Recorder(ok({}))
    async with make(rec) as c:
        vecs = await c.embed(["", ""])
    assert rec.requests == [] and len(vecs) == 2 and len(vecs[0]) == 1024


async def test_rerank_shape_sorted(conn):
    rec = Recorder(ok({"results": [{"index": 2, "relevance_score": 0.01}, {"index": 0, "relevance_score": 0.19}],
                       "usage": {"cost": 9.9e-5}}))
    async with make(rec, conn) as c:
        out = await c.rerank("질의", ["a", "b", "c"], top_n=2)
    assert out == [(0, 0.19), (2, 0.01)]
    b = rec.body()
    assert b == {"model": RERANK_MODEL, "query": "질의", "documents": ["a", "b", "c"], "top_n": 2,
                 "return_documents": False}
    assert rec.requests[0].url.path.endswith("/rerank")
    assert rows(conn)[0][3:5] == (9.9e-5, "REPORTED")


async def test_decide_shape():
    answers = {"project": {"choice": "mirae", "probabilities": {"mirae": 0.97}, "confidence": 0.96}}
    rec = Recorder(ok({"answers": answers, "usage": {"cost": 1.7e-5}}))
    q = {"project": {"type": "choice", "instructions": "어느 과제?", "criteria": {"mirae": "누리은행"}}}
    async with make(rec) as c:
        out = await c.decide({"file": "장애보고서.docx"}, q)
        assert c.total_cost_usd == pytest.approx(1.7e-5)
    assert out == answers
    assert rec.body() == {"model": DECISION_MODEL, "state": {"file": "장애보고서.docx"}, "questions": q}
    assert rec.requests[0].headers["authorization"] == f"Bearer {KEY}"


async def test_chat_json_strict_and_estimated_cost(conn):
    rec = Recorder(ok(CHAT_OK))
    schema = {"type": "object", "properties": {"decision": {"type": "string"}}, "required": ["decision"],
              "additionalProperties": False}
    async with make(rec, conn) as c:
        out = await c.chat_json([{"role": "user", "content": "hi"}], schema, name="node_judgment")
    assert out == {"decision": "NODE", "node_id": "doc.08.01"}
    b = rec.body()
    assert b["model"] == "liner-gpt-router" and b["routing_strategy"] == "cost"
    assert b["max_completion_tokens"] == 400
    assert b["response_format"] == {"type": "json_schema",
                                    "json_schema": {"name": "node_judgment", "strict": True, "schema": schema}}
    expected = (800 * 0.10 + 200 * 0.01 + 100 * 0.50) / 1e6
    (ep, model, rid, cost, kind, _), = rows(conn)
    assert (ep, model, kind) == ("/chat/completions", "gpt-6-luna-2026-10-01", "ESTIMATED")
    assert cost == pytest.approx(expected)


async def test_chat_json_invalid_raises_and_not_cached(conn):
    bad = json.loads(json.dumps(CHAT_OK))
    bad["choices"][0]["message"]["content"] = '{"decision": "NO'
    bad["choices"][0]["finish_reason"] = "length"
    rec = Recorder(ok(bad))
    async with make(rec, conn) as c:
        with pytest.raises(LinerError) as ei:
            await c.chat_json([{"role": "user", "content": "x"}], {"type": "object"}, name="n")
    assert ei.value.code == "invalid_json"
    assert conn.execute("SELECT COUNT(*) FROM response_cache").fetchone()[0] == 0


async def test_credits_usd(conn):
    rec = Recorder(ok({"balance": 4990000, "balance_usd": "4.99", "currency": "USD"}))
    async with make(rec, conn) as c:
        assert await c.credits_usd() == pytest.approx(4.99)
    assert rec.requests[0].method == "GET"
    assert rows(conn)[0][3:5] == (0.0, "NONE")


# ---------------------------------------------------------------- guards & retries
async def test_host_guard_refuses_other_host():
    rec = Recorder(ok({}))
    async with make(rec) as c:
        with pytest.raises(LinerError) as ei:
            await c._http.get("https://evil.example.com/x")
        assert ei.value.code == "host_refused"
        with pytest.raises(LinerError):
            await c._http.post("http://platform.liner.com.evil.example.com/api/v1/rerank", json={})
    assert rec.requests == []


async def test_retry_on_429_honours_retry_after():
    rec = Recorder(
        httpx.Response(429, json={"error": {"code": "rate_limited", "message": "slow", "retryable": True}},
                       headers={"retry-after": "5"}),
        ok({"answers": {"q": {}}, "usage": {"cost": 0}}),
    )
    async with make(rec) as c:
        out = await c.decide("s", {"q": {}})
        assert out == {"q": {}}
        assert len(c.sleeps) == 1 and 5.0 <= c.sleeps[0] <= 5.3
    assert len(rec.requests) == 2


async def test_retry_500_backoff_then_give_up():
    rec = Recorder(httpx.Response(500, json={"error": {"message": "boom"}}))
    async with make(rec) as c:
        with pytest.raises(LinerError) as ei:
            await c.rerank("q", ["a"])
        assert ei.value.status == 500
        assert [round(s) for s in c.sleeps] == [1, 2, 4]
    assert len(rec.requests) == 4


async def test_no_retry_on_400():
    rec = Recorder(httpx.Response(400, json={"error": {"message": "bad", "type": "invalid_request_error",
                                                       "code": "invalid_value", "retryable": True}}))
    async with make(rec) as c:
        with pytest.raises(LinerError) as ei:
            await c.rerank("q", ["a"])
        assert ei.value.status == 400 and ei.value.code == "invalid_value"
        assert c.sleeps == []
    assert len(rec.requests) == 1


async def test_402_insufficient_credit_and_key_masked():
    rec = Recorder(httpx.Response(402, json={"error": {"code": "payment_required",
                                                       "message": f"no credit for key {KEY}"}}))
    async with make(rec) as c:
        with pytest.raises(LinerError) as ei:
            await c.rerank("질의", ["후보"])
        assert c.sleeps == []
    assert ei.value.status == 402 and ei.value.code == "insufficient_credit"
    assert KEY not in str(ei.value) and "***" in str(ei.value)
    assert len(rec.requests) == 1


# ---------------------------------------------------------------- cache
async def test_cache_hit_avoids_network(conn):
    rec = Recorder(ok({"answers": {"q": {"choice": "a"}}, "usage": {"cost": 2e-5}}))
    async with make(rec, conn) as c:
        a1 = await c.decide({"x": 1, "y": 2}, {"q": {}})
        a2 = await c.decide({"y": 2, "x": 1}, {"q": {}})  # 키 순서만 다름 → 같은 캐시 키
        assert c.total_cost_usd == pytest.approx(2e-5)
    assert a1 == a2 and len(rec.requests) == 1
    kinds = [r[4] for r in rows(conn)]
    assert kinds == ["REPORTED", "CACHED"]
    assert rows(conn)[1][3] == 0.0


async def test_use_cache_false_calls_twice(conn):
    rec = Recorder(ok({"results": [], "usage": {"cost": 1e-6}}))
    async with make(rec, conn, use_cache=False) as c:
        await c.rerank("q", ["a"])
        await c.rerank("q", ["a"])
    assert len(rec.requests) == 2
    assert [r[4] for r in rows(conn)] == ["REPORTED", "REPORTED"]


# ---------------------------------------------------------------- pricing
def test_pricing_unknown_model_uses_fallback(caplog):
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    with caplog.at_level("WARNING", logger="tongdory2.liner"):
        cost = estimate_chat_cost("mystery-model-9", usage)
    assert cost == pytest.approx(FALLBACK_PRICE.input)
    assert "unknown chat model" in caplog.text


def test_pricing_prefix_match():
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    assert estimate_chat_cost("openai/gpt-6.1-sol", usage) == pytest.approx(2.0)
    assert estimate_chat_cost("gpt-6-sol-2026", usage) == pytest.approx(2.0)
    assert estimate_chat_cost("gpt-6.1-terra", usage) == pytest.approx(1.0)

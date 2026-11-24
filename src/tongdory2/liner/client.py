"""Liner API 비동기 클라이언트 (03 §5).

- 재시도: 429/500/502 또는 retryable=true → max(Retry-After, 1·2·4s) + jitter, 최대 3회.
  400/401/402/413 또는 retryable=false → 즉시 LinerError.
- 호스트 가드: base_url 호스트 외 요청 거부.
- 비용 기록: conn이 있으면 api_calls에 호출마다 1행 (REPORTED/ESTIMATED/FLAT/NONE/CACHED).
- 응답 캐시: conn and use_cache → response_cache (sha256(endpoint + 정규화 body + model)).
- API 키는 로그·오류 문구에 절대 노출하지 않는다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import sqlite3
import time
from typing import Any

import httpx

from tongdory2.db import new_id, now_iso
from tongdory2.liner.pricing import estimate_chat_cost

log = logging.getLogger("tongdory2.liner")

EMBED_MODEL = "baai/bge-m3"
RERANK_MODEL = "qwen/qwen3-reranker-8b"
DECISION_MODEL = "typesafe/jev-1.14"
CHAT_MODEL = "liner-gpt-router"
WEB_SEARCH = "/tools/search/web"

EMBED_BATCH = 256
EMBED_DIMS_FALLBACK = {"baai/bge-m3": 1024, "openai/text-embedding-3-small": 1536}
MAX_RETRIES = 3
BACKOFF_S = (1.0, 2.0, 4.0)
RETRY_STATUSES = {429, 500, 502}
NO_RETRY_STATUSES = {400, 401, 402, 413}

# 엔드포인트별 읽기 타임아웃(s), 03 §5.2
READ_TIMEOUTS = {
    "/chat/completions": 60.0,
    "/embeddings": 30.0,
    "/rerank": 20.0,
    "/decisions": 60.0,
    WEB_SEARCH: 20.0,
    "/credits/balance": 5.0,
}
CONNECT_TIMEOUT = 5.0

API_CALLS_DDL = """
CREATE TABLE IF NOT EXISTS api_calls(
  call_id TEXT PRIMARY KEY,
  run_id TEXT,
  endpoint TEXT,
  model TEXT,
  request_id TEXT,
  cost_usd REAL,
  cost_kind TEXT CHECK(cost_kind IN('REPORTED','ESTIMATED','FLAT','NONE','CACHED')),
  latency_ms INTEGER,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS api_calls_by_run ON api_calls(run_id);
"""
RESPONSE_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS response_cache(
  key TEXT PRIMARY KEY,
  body TEXT,
  created_at TEXT
);
"""


class LinerError(RuntimeError):
    def __init__(self, message: str, status: int = 0, code: str = "error", retryable: bool | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.status} {self.code}] {self.args[0] if self.args else ''}"


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def cache_key(endpoint: str, body: dict | None, model: str | None) -> str:
    raw = endpoint + "\n" + _canonical(body or {}) + "\n" + (model or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


class LinerClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        conn: sqlite3.Connection | None = None,
        run_id: str | None = None,
        use_cache: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise LinerError("LINER_API_KEY is empty", status=0, code="no_api_key")
        self._key = api_key
        self.base_url = base_url.rstrip("/")
        self._host = httpx.URL(self.base_url).host
        self.conn = conn
        self.run_id = run_id
        self.use_cache = use_cache and conn is not None
        self.total_cost_usd = 0.0
        self._sleep = asyncio.sleep  # 테스트에서 교체
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(60.0, connect=CONNECT_TIMEOUT),
            event_hooks={"request": [self._guard_hook]},
        )
        self._sems = {
            "/embeddings": asyncio.Semaphore(4),
            "/rerank": asyncio.Semaphore(4),
            "/decisions": asyncio.Semaphore(4),
            "/chat/completions": asyncio.Semaphore(8),
        }
        if conn is not None:
            with conn:
                conn.executescript(API_CALLS_DDL + RESPONSE_CACHE_DDL)

    # ------------------------------------------------------------------ lifecycle
    async def __aenter__(self) -> "LinerClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ helpers
    def _mask(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    def _check_host(self, url: httpx.URL) -> None:
        if url.host != self._host:
            raise LinerError(f"host guard: refused request to {url.host!r}", status=0, code="host_refused")

    async def _guard_hook(self, request: httpx.Request) -> None:
        self._check_host(request.url)

    def _headers(self, endpoint: str) -> dict[str, str]:
        if endpoint == WEB_SEARCH:
            return {"x-api-key": self._key, "Content-Type": "application/json"}
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

    def _record(
        self, endpoint: str, model: str | None, request_id: str | None, cost: float, kind: str, latency_ms: int
    ) -> None:
        self.total_cost_usd += cost
        if self.conn is None:
            return
        with self.conn:
            self.conn.execute(
                "INSERT INTO api_calls(call_id, run_id, endpoint, model, request_id, cost_usd, cost_kind,"
                " latency_ms, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id("call"), self.run_id, endpoint, model, request_id, cost, kind, latency_ms, now_iso()),
            )

    def _cache_get(self, key: str) -> dict | None:
        if not self.use_cache:
            return None
        row = self.conn.execute("SELECT body FROM response_cache WHERE key=?", (key,)).fetchone()  # type: ignore[union-attr]
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None

    def _cache_put(self, key: str, data: dict) -> None:
        if not self.use_cache:
            return
        with self.conn:  # type: ignore[union-attr]
            self.conn.execute(  # type: ignore[union-attr]
                "INSERT OR REPLACE INTO response_cache(key, body, created_at) VALUES(?,?,?)",
                (key, json.dumps(data, ensure_ascii=False), now_iso()),
            )

    def _error_from(self, resp: httpx.Response) -> LinerError:
        code = f"http_{resp.status_code}"
        message = ""
        retryable: bool | None = None
        try:
            body = resp.json()
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                code = str(err.get("code") or err.get("type") or code)
                message = str(err.get("message") or "")
                if isinstance(err.get("retryable"), bool):
                    retryable = err["retryable"]
            elif isinstance(err, str):
                message = err
        except (json.JSONDecodeError, ValueError):
            message = resp.text[:300]
        if resp.status_code == 402:
            code = "insufficient_credit"
        return LinerError(self._mask(message or resp.reason_phrase), status=resp.status_code, code=code,
                          retryable=retryable)

    async def _send(self, method: str, endpoint: str, body: dict | None) -> tuple[dict, str | None]:
        """재시도 포함 HTTP 호출. (json, request_id) 반환."""
        url = httpx.URL(self.base_url + endpoint)
        self._check_host(url)
        timeout = httpx.Timeout(READ_TIMEOUTS.get(endpoint, 60.0), connect=CONNECT_TIMEOUT)
        attempt = 0
        while True:
            retry_after: float | None = None
            try:
                resp = await self._http.request(method, url, headers=self._headers(endpoint), json=body,
                                                timeout=timeout)
            except LinerError:
                raise
            except httpx.TransportError as exc:
                err = LinerError(self._mask(f"network error: {type(exc).__name__}: {exc}"), status=0,
                                 code="network", retryable=True)
            else:
                ignored = resp.headers.get("x-liner-ignored-parameters")
                if ignored:
                    log.info("liner %s ignored parameters: %s", endpoint, ignored)
                if resp.status_code < 400:
                    try:
                        data = resp.json()
                    except (json.JSONDecodeError, ValueError) as exc:
                        raise LinerError(f"non-JSON response from {endpoint}", status=resp.status_code,
                                         code="bad_response") from exc
                    return data, resp.headers.get("x-request-id")
                err = self._error_from(resp)
                retry_after = _parse_retry_after(resp.headers.get("retry-after"))

            if err.status in NO_RETRY_STATUSES or err.retryable is False:
                can_retry = False
            else:
                can_retry = err.retryable is True or err.status in RETRY_STATUSES
            if not can_retry or attempt >= MAX_RETRIES:
                log.warning("liner %s failed: status=%s code=%s", endpoint, err.status, err.code)
                raise err
            delay = max(retry_after or 0.0, BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]) + random.uniform(0, 0.25)
            log.info("liner %s retry %d/%d in %.2fs (status=%s)", endpoint, attempt + 1, MAX_RETRIES, delay,
                     err.status)
            attempt += 1
            await self._sleep(delay)

    async def _call(self, endpoint: str, body: dict, model: str | None, *, cacheable: bool = True,
                    validate: Any = None) -> dict:
        """캐시 → (세마포어) 호출 → 비용 기록."""
        key = cache_key(endpoint, body, model) if cacheable else None
        if key is not None:
            hit = self._cache_get(key)
            if hit is not None:
                self._record(endpoint, model, None, 0.0, "CACHED", 0)
                return hit
        sem = self._sems.get(endpoint)
        t0 = time.perf_counter()
        if sem is not None:
            async with sem:
                data, request_id = await self._send("POST", endpoint, body)
        else:
            data, request_id = await self._send("POST", endpoint, body)
        latency = int((time.perf_counter() - t0) * 1000)
        cost, kind = self._cost_of(endpoint, data)
        self._record(endpoint, data.get("model") or model, request_id, cost, kind, latency)
        if validate is not None:
            validate(data)  # 검증 실패 응답은 캐시하지 않는다
        if key is not None:
            self._cache_put(key, data)
        return data

    @staticmethod
    def _cost_of(endpoint: str, data: dict) -> tuple[float, str]:
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        if usage.get("cost") is not None:
            try:
                return float(usage["cost"]), "REPORTED"
            except (TypeError, ValueError):
                pass
        if endpoint == "/chat/completions":
            return estimate_chat_cost(data.get("model"), usage), "ESTIMATED"
        if endpoint == WEB_SEARCH:
            return 0.001, "FLAT"
        if endpoint == "/rerank":
            units = (data.get("meta") or {}).get("billed_units", {}).get("search_units") or usage.get("search_units")
            if units:
                return float(units) * 0.001, "ESTIMATED"
        return 0.0, "NONE"

    # ------------------------------------------------------------------ endpoints
    async def embed(self, texts: list[str], model: str = EMBED_MODEL) -> list[list[float]]:
        """빈 문자열은 전송하지 않고 영벡터로 채워 인덱스 정렬을 유지한다."""
        keep = [i for i, t in enumerate(texts) if isinstance(t, str) and t.strip()]
        batches = [keep[i:i + EMBED_BATCH] for i in range(0, len(keep), EMBED_BATCH)]

        async def run(idx: list[int]) -> list[list[float]]:
            data = await self._call("/embeddings", {"model": model, "input": [texts[i] for i in idx]}, model)
            rows = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
            vecs = [r["embedding"] for r in rows]
            if len(vecs) != len(idx):
                raise LinerError(f"embeddings count mismatch: {len(vecs)} != {len(idx)}", status=200,
                                 code="bad_response")
            return vecs

        results = await asyncio.gather(*(run(b) for b in batches))
        out: list[list[float] | None] = [None] * len(texts)
        dim = 0
        for idx, vecs in zip(batches, results):
            for i, v in zip(idx, vecs):
                out[i] = v
                dim = dim or len(v)
        dim = dim or EMBED_DIMS_FALLBACK.get(model, 1024)
        return [v if v is not None else [0.0] * dim for v in out]

    async def rerank(
        self, query: str, documents: list[str], top_n: int = 5, model: str = RERANK_MODEL
    ) -> list[tuple[int, float]]:
        if not documents:
            return []
        body = {"model": model, "query": query, "documents": documents,
                "top_n": min(top_n, len(documents)), "return_documents": False}
        data = await self._call("/rerank", body, model)
        pairs = [(int(r["index"]), float(r["relevance_score"])) for r in data.get("results") or []]
        return sorted(pairs, key=lambda p: p[1], reverse=True)

    async def decide(self, state: dict | str, questions: dict, model: str = DECISION_MODEL) -> dict:
        body = {"model": model, "state": state, "questions": questions}
        def check(d: dict) -> None:
            if not isinstance(d.get("answers"), dict):
                raise LinerError("decisions response has no answers", status=200, code="bad_response")

        data = await self._call("/decisions", body, model, validate=check)
        return data["answers"]

    async def search_web(self, query: str) -> list[dict]:
        data = await self._call(WEB_SEARCH, {"query": query, "max_results": 3, "include_knowledge": False}, None)
        return list(data.get("results") or [])

    async def chat_json(
        self,
        messages: list[dict],
        schema: dict,
        name: str,
        routing: str = "cost",
        max_tokens: int = 400,
        model: str = CHAT_MODEL,
        reasoning_effort: str | None = None,
    ) -> dict:
        if "schema" in schema and "type" not in schema:  # {name, strict, schema} 래퍼도 허용
            schema = schema["schema"]
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "routing_strategy": routing,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": name, "strict": True, "schema": schema}},
            "max_completion_tokens": max_tokens,
        }
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        data = await self._call("/chat/completions", body, model, validate=self._parse_chat_json)
        return self._parse_chat_json(data)

    @staticmethod
    def _parse_chat_json(data: dict) -> dict:
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content")
        except (KeyError, IndexError, TypeError) as exc:
            raise LinerError("chat response has no choices", status=200, code="bad_response") from exc
        if not content:
            raise LinerError(f"chat returned empty content (finish_reason={choice.get('finish_reason')})",
                             status=200, code="invalid_json")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LinerError(f"chat content is not valid JSON (finish_reason={choice.get('finish_reason')})",
                             status=200, code="invalid_json") from exc
        if not isinstance(parsed, dict):
            raise LinerError("chat JSON is not an object", status=200, code="invalid_json")
        return parsed

    async def credits_usd(self) -> float:
        t0 = time.perf_counter()
        data, request_id = await self._send("GET", "/credits/balance", None)
        self._record("/credits/balance", None, request_id, 0.0, "NONE", int((time.perf_counter() - t0) * 1000))
        try:
            return float(data["balance_usd"])
        except (KeyError, TypeError, ValueError):
            if "balance" in data:  # micro-USD 정수
                return float(data["balance"]) / 1_000_000
            raise LinerError("credits response has no balance_usd", status=200, code="bad_response")

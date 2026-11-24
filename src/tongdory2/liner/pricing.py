"""Chat 모델 단가표 (USD / 1M tokens, Liner Pricing 2026-11-17, 03 §5.3).

Chat 응답에는 usage.cost가 없으므로(실측) 응답 `model` × 토큰으로 추정한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("tongdory2.liner")


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cached: float | None = None  # None → input 단가 적용
    cache_write: float | None = None  # None → input 단가 적용


CHAT_PRICES: dict[str, Price] = {
    "gpt-6-luna": Price(0.10, 0.50, cached=0.01, cache_write=0.125),
    "gpt-6.1-terra": Price(1.0, 5.0),
    "gpt-6.1-sol": Price(2.0, 10.0),
    "gpt-6-sol": Price(2.0, 10.0),
    "gpt-6-astra": Price(10.0, 50.0),
    "claude-haiku-5.5": Price(0.10, 0.50),
    "claude-sonnet-5.5": Price(2.0, 10.0),
    "claude-opus-5.5": Price(4.0, 20.0),
    "gemini-3.5-flash-lite": Price(0.30, 2.50),
    "gemini-3.8-flash": Price(0.75, 3.75),
}

# 미등록 모델 → 가장 비싼 라우터 모델 단가로 보수 추정
FALLBACK_PRICE: Price = max(CHAT_PRICES.values(), key=lambda p: p.input + p.output)


def lookup_price(model: str | None) -> tuple[Price, bool]:
    """응답 모델명(예: 'openai/gpt-6-luna-2026-10-01')에서 단가를 찾는다. (price, known)."""
    name = (model or "").lower().rsplit("/", 1)[-1]
    # 가장 긴 키 우선 (gpt-6.1-sol vs gpt-6-sol 혼동 방지)
    for key in sorted(CHAT_PRICES, key=len, reverse=True):
        if name == key or name.startswith(key + "-") or name.startswith(key + "@") or name.startswith(key + ":"):
            return CHAT_PRICES[key], True
    return FALLBACK_PRICE, False


def estimate_chat_cost(model: str | None, usage: dict | None) -> float:
    usage = usage or {}
    price, known = lookup_price(model)
    if not known:
        log.warning("pricing: unknown chat model %r, using most expensive router price", model)
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    details = usage.get("prompt_tokens_details") or {}
    cached = int(details.get("cached_tokens") or 0)
    cache_write = int(details.get("cache_write_tokens") or details.get("cache_creation_tokens") or 0)
    plain = max(prompt - cached - cache_write, 0)
    cost = (
        plain * price.input
        + cached * (price.cached if price.cached is not None else price.input)
        + cache_write * (price.cache_write if price.cache_write is not None else price.input)
        + completion * price.output
    ) / 1_000_000
    return cost

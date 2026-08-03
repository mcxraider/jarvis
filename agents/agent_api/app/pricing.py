"""Exact, provider-aware token pricing for durable usage telemetry.

Pricing is deliberately conservative: a call is costed only when its provider,
returned model, and per-request pricing tier are known.  Unknown combinations
return ``None`` instead of borrowing the price of a similarly named model.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Optional


TOKENS_PER_RATE_UNIT = Decimal("1000000")
USD_QUANTUM = Decimal("0.0001")
OPENAI_LONG_CONTEXT_THRESHOLD = 272_000
OPENAI_PRICING_AS_OF = "2026-08-02"
DEEPSEEK_PRICING_AS_OF = "2026-07-05"


@dataclass(frozen=True)
class TokenRates:
    """USD prices per one million tokens for one provider/model/tier."""

    cached_input: Decimal
    uncached_input: Decimal
    output: Decimal
    cache_write: Optional[Decimal] = None
    source: str = ""
    as_of: str = ""


_OPENAI_PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
_DEEPSEEK_PRICING_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing"

# DeepSeek API prices retained from the existing production table.  These are
# provider keyed so a foreign provider can never inherit them by model alone.
DEEPSEEK_TOKEN_RATES: Mapping[str, TokenRates] = {
    "deepseek-v4-flash": TokenRates(
        cached_input=Decimal("0.0028"),
        uncached_input=Decimal("0.14"),
        output=Decimal("0.28"),
        source=_DEEPSEEK_PRICING_SOURCE,
        as_of=DEEPSEEK_PRICING_AS_OF,
    ),
    "deepseek-v4-pro": TokenRates(
        cached_input=Decimal("0.003625"),
        uncached_input=Decimal("0.435"),
        output=Decimal("0.87"),
        source=_DEEPSEEK_PRICING_SOURCE,
        as_of=DEEPSEEK_PRICING_AS_OF,
    ),
}


def _openai_rates(input_rate: str, output_rate: str, *, long_context: bool) -> TokenRates:
    input_price = Decimal(input_rate)
    output_price = Decimal(output_rate)
    if long_context:
        input_price *= Decimal("2")
        output_price *= Decimal("1.5")
    return TokenRates(
        cached_input=input_price * Decimal("0.1"),
        uncached_input=input_price,
        cache_write=input_price * Decimal("1.25"),
        output=output_price,
        source=_OPENAI_PRICING_SOURCE,
        as_of=OPENAI_PRICING_AS_OF,
    )


# OpenAI states that prompts over 272K input tokens use the long-context price
# for the entire request.  Therefore tier resolution is per call, never after
# aggregating usage across a run.
OPENAI_TOKEN_RATES: Mapping[tuple[str, str], TokenRates] = {
    ("gpt-5.6-luna", "standard"): _openai_rates("1", "6", long_context=False),
    ("gpt-5.6-luna", "long_context"): _openai_rates("1", "6", long_context=True),
    ("gpt-5.6-terra", "standard"): _openai_rates("2.5", "15", long_context=False),
    ("gpt-5.6-terra", "long_context"): _openai_rates("2.5", "15", long_context=True),
    ("gpt-5.6-sol", "standard"): _openai_rates("5", "30", long_context=False),
    ("gpt-5.6-sol", "long_context"): _openai_rates("5", "30", long_context=True),
}


def derive_uncached_input_tokens(
    prompt_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int = 0,
) -> int:
    """Validate input token details and return ordinary cache-miss tokens."""

    if prompt_tokens < 0 or cached_tokens < 0 or cache_write_tokens < 0:
        raise ValueError("Token counts must be non-negative.")
    if cached_tokens + cache_write_tokens > prompt_tokens:
        raise ValueError(
            "Cached-read and cache-write tokens cannot exceed total input tokens."
        )
    return prompt_tokens - cached_tokens - cache_write_tokens


def pricing_tier_for_request(provider: str, request_input_tokens: Optional[int]) -> Optional[str]:
    """Resolve a verified per-request tier, or ``None`` when it is unknowable."""

    normalized_provider = str(provider).strip().lower()
    if request_input_tokens is None or request_input_tokens < 0:
        return None
    if normalized_provider == "openai":
        return (
            "long_context"
            if request_input_tokens > OPENAI_LONG_CONTEXT_THRESHOLD
            else "standard"
        )
    if normalized_provider == "deepseek":
        return "standard"
    return None


def _rates_for(provider: str, model: str, pricing_tier: Optional[str]) -> Optional[TokenRates]:
    normalized_provider = str(provider).strip().lower()
    if normalized_provider == "deepseek" and pricing_tier in {None, "standard"}:
        return DEEPSEEK_TOKEN_RATES.get(model)
    if normalized_provider == "openai" and pricing_tier is not None:
        return OPENAI_TOKEN_RATES.get((model, pricing_tier))
    return None


def calculate_call_cost_usd(
    *,
    provider: str,
    model: str,
    prompt_tokens: int,
    cached_read_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
    request_input_tokens: Optional[int],
    pricing_tier: Optional[str] = None,
) -> Optional[Decimal]:
    """Calculate one call before aggregation.

    Reasoning tokens are already included in Chat Completions output tokens and
    must not be charged a second time.
    """

    if output_tokens < 0:
        raise ValueError("Token counts must be non-negative.")
    tier = pricing_tier or pricing_tier_for_request(provider, request_input_tokens)
    rates = _rates_for(provider, model, tier)
    if rates is None:
        return None
    uncached_tokens = derive_uncached_input_tokens(
        prompt_tokens,
        cached_read_tokens,
        cache_write_tokens,
    )
    if cache_write_tokens and rates.cache_write is None:
        # A reported charge category without a maintained rate is unknowable.
        return None
    write_rate = rates.cache_write or Decimal("0")
    cost_usd = (
        Decimal(cached_read_tokens) * rates.cached_input
        + Decimal(cache_write_tokens) * write_rate
        + Decimal(uncached_tokens) * rates.uncached_input
        + Decimal(output_tokens) * rates.output
    ) / TOKENS_PER_RATE_UNIT
    return cost_usd.quantize(USD_QUANTUM, rounding=ROUND_HALF_UP)


def calculate_usage_record_cost_usd(record: Any) -> Optional[Decimal]:
    """Cost a typed usage record without coupling pricing to its implementation."""

    provider = getattr(record, "provider", "")
    provider_value = getattr(provider, "value", provider)
    return calculate_call_cost_usd(
        provider=str(provider_value),
        model=str(getattr(record, "returned_model", "")),
        prompt_tokens=int(getattr(record, "prompt_tokens")),
        cached_read_tokens=int(getattr(record, "cached_read_tokens")),
        cache_write_tokens=int(getattr(record, "cache_write_tokens")),
        output_tokens=int(getattr(record, "completion_tokens")),
        request_input_tokens=getattr(record, "request_input_tokens", None),
        pricing_tier=getattr(record, "pricing_tier", None),
    )


def calculate_ledger_cost_usd(records: Iterable[Any]) -> Optional[Decimal]:
    """Sum exact per-call costs, returning ``None`` if any call is unpriceable."""

    total = Decimal("0")
    for record in records:
        cost = calculate_usage_record_cost_usd(record)
        if cost is None:
            return None
        total += cost
    return total.quantize(USD_QUANTUM, rounding=ROUND_HALF_UP)


def calculate_cost_usd(
    model: str,
    prompt_tokens: int,
    cached_tokens: int,
    output_tokens: int,
) -> Optional[Decimal]:
    """Backward-compatible DeepSeek cost helper for aggregate-only callers."""

    return calculate_call_cost_usd(
        provider="deepseek",
        model=model,
        prompt_tokens=prompt_tokens,
        cached_read_tokens=cached_tokens,
        cache_write_tokens=0,
        output_tokens=output_tokens,
        request_input_tokens=prompt_tokens,
        pricing_tier="standard",
    )


__all__ = [
    "DEEPSEEK_PRICING_AS_OF",
    "DEEPSEEK_TOKEN_RATES",
    "OPENAI_LONG_CONTEXT_THRESHOLD",
    "OPENAI_PRICING_AS_OF",
    "OPENAI_TOKEN_RATES",
    "TokenRates",
    "calculate_call_cost_usd",
    "calculate_cost_usd",
    "calculate_ledger_cost_usd",
    "calculate_usage_record_cost_usd",
    "derive_uncached_input_tokens",
    "pricing_tier_for_request",
]

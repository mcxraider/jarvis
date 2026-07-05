"""Provider pricing used to convert token usage into durable cost telemetry."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Optional


TOKENS_PER_RATE_UNIT = Decimal("1000000")
USD_QUANTUM = Decimal("0.0001")


@dataclass(frozen=True)
class TokenRates:
    """USD prices per one million tokens for one model."""

    cached_input: Decimal
    uncached_input: Decimal
    output: Decimal


# DeepSeek API pricing as of 2026-07-05. Keep rates as strings so Decimal never
# inherits binary floating-point error when this table is updated.
DEEPSEEK_TOKEN_RATES: Mapping[str, TokenRates] = {
    "deepseek-v4-flash": TokenRates(
        cached_input=Decimal("0.0028"),
        uncached_input=Decimal("0.14"),
        output=Decimal("0.28"),
    ),
    "deepseek-v4-pro": TokenRates(
        cached_input=Decimal("0.003625"),
        uncached_input=Decimal("0.435"),
        output=Decimal("0.87"),
    ),
}


def derive_uncached_input_tokens(prompt_tokens: int, cached_tokens: int) -> int:
    """Validate a prompt/cache pair and return the cache-miss token count."""

    if prompt_tokens < 0 or cached_tokens < 0:
        raise ValueError("Token counts must be non-negative.")
    if cached_tokens > prompt_tokens:
        raise ValueError("Cached input tokens cannot exceed total input tokens.")
    return prompt_tokens - cached_tokens


def calculate_cost_usd(
    model: str,
    prompt_tokens: int,
    cached_tokens: int,
    output_tokens: int,
) -> Optional[Decimal]:
    """Calculate run cost in USD, rounded to four decimal places.

    Returns ``None`` when the model has no maintained pricing entry. Invalid
    token metadata raises ``ValueError`` so callers cannot record a misleading
    cost.
    """

    if output_tokens < 0:
        raise ValueError("Token counts must be non-negative.")
    uncached_tokens = derive_uncached_input_tokens(prompt_tokens, cached_tokens)
    rates = DEEPSEEK_TOKEN_RATES.get(model)
    if rates is None:
        return None

    cost_usd = (
        Decimal(cached_tokens) * rates.cached_input
        + Decimal(uncached_tokens) * rates.uncached_input
        + Decimal(output_tokens) * rates.output
    ) / TOKENS_PER_RATE_UNIT
    return cost_usd.quantize(USD_QUANTUM, rounding=ROUND_HALF_UP)


__all__ = [
    "DEEPSEEK_TOKEN_RATES",
    "TokenRates",
    "calculate_cost_usd",
    "derive_uncached_input_tokens",
]

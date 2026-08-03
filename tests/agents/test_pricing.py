"""Tests for DeepSeek token pricing."""

from decimal import Decimal

import pytest

from agents.agent_api.app.pricing import (
    OPENAI_LONG_CONTEXT_THRESHOLD,
    calculate_call_cost_usd,
    calculate_cost_usd,
    calculate_ledger_cost_usd,
    derive_uncached_input_tokens,
    pricing_tier_for_request,
)


@pytest.mark.parametrize(
    ("prompt_tokens", "cached_tokens", "output_tokens", "expected"),
    [
        (1_000_000, 1_000_000, 0, Decimal("0.0028")),
        (1_000_000, 0, 0, Decimal("0.1400")),
        (0, 0, 1_000_000, Decimal("0.2800")),
        (100, 40, 10, Decimal("0.0000")),
        (1, 1, 0, Decimal("0.0000")),
        (2, 2, 0, Decimal("0.0000")),
    ],
)
def test_flash_costs(
    prompt_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    expected: Decimal,
) -> None:
    assert (
        calculate_cost_usd(
            "deepseek-v4-flash",
            prompt_tokens,
            cached_tokens,
            output_tokens,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("prompt_tokens", "cached_tokens", "output_tokens", "expected"),
    [
        (1_000_000, 1_000_000, 0, Decimal("0.0036")),
        (1_000_000, 0, 0, Decimal("0.4350")),
        (0, 0, 1_000_000, Decimal("0.8700")),
        (100, 40, 10, Decimal("0.0000")),
    ],
)
def test_pro_costs(
    prompt_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    expected: Decimal,
) -> None:
    assert (
        calculate_cost_usd(
            "deepseek-v4-pro",
            prompt_tokens,
            cached_tokens,
            output_tokens,
        )
        == expected
    )


def test_unknown_model_has_no_cost() -> None:
    assert calculate_cost_usd("future-model", 100, 20, 10) is None


@pytest.mark.parametrize(
    ("prompt_tokens", "cached_tokens", "output_tokens"),
    [
        (-1, 0, 0),
        (10, -1, 0),
        (10, 11, 0),
        (0, 0, -1),
    ],
)
def test_invalid_token_metadata_is_rejected(
    prompt_tokens: int,
    cached_tokens: int,
    output_tokens: int,
) -> None:
    with pytest.raises(ValueError):
        calculate_cost_usd(
            "deepseek-v4-flash",
            prompt_tokens,
            cached_tokens,
            output_tokens,
        )


def test_uncached_tokens_are_derived_from_total_input() -> None:
    assert derive_uncached_input_tokens(150, 40) == 110


def test_cache_writes_are_separated_from_ordinary_uncached_input() -> None:
    assert derive_uncached_input_tokens(150, 40, 30) == 80


@pytest.mark.parametrize(
    ("model", "input_rate", "cached_rate", "write_rate", "output_rate"),
    [
        ("gpt-5.6-luna", "1.0000", "0.1000", "1.2500", "6.0000"),
        ("gpt-5.6-terra", "2.5000", "0.2500", "3.1250", "15.0000"),
        ("gpt-5.6-sol", "5.0000", "0.5000", "6.2500", "30.0000"),
    ],
)
def test_openai_standard_call_categories(
    model: str,
    input_rate: str,
    cached_rate: str,
    write_rate: str,
    output_rate: str,
) -> None:
    common = {
        "provider": "openai",
        "model": model,
        "request_input_tokens": 1_000_000,
        "pricing_tier": "standard",
    }
    assert calculate_call_cost_usd(
        **common,
        prompt_tokens=1_000_000,
        cached_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=0,
    ) == Decimal(input_rate)
    assert calculate_call_cost_usd(
        **common,
        prompt_tokens=1_000_000,
        cached_read_tokens=1_000_000,
        cache_write_tokens=0,
        output_tokens=0,
    ) == Decimal(cached_rate)
    assert calculate_call_cost_usd(
        **common,
        prompt_tokens=1_000_000,
        cached_read_tokens=0,
        cache_write_tokens=1_000_000,
        output_tokens=0,
    ) == Decimal(write_rate)
    assert calculate_call_cost_usd(
        **common,
        prompt_tokens=0,
        cached_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=1_000_000,
    ) == Decimal(output_rate)


def test_openai_long_context_tier_applies_to_entire_call() -> None:
    assert pricing_tier_for_request("openai", OPENAI_LONG_CONTEXT_THRESHOLD) == "standard"
    assert (
        pricing_tier_for_request("openai", OPENAI_LONG_CONTEXT_THRESHOLD + 1)
        == "long_context"
    )
    assert calculate_call_cost_usd(
        provider="openai",
        model="gpt-5.6-luna",
        prompt_tokens=1_000_000,
        cached_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=1_000_000,
        request_input_tokens=OPENAI_LONG_CONTEXT_THRESHOLD + 1,
    ) == Decimal("11.0000")


@pytest.mark.parametrize(
    ("provider", "model", "tier"),
    [
        ("openai", "future-model", "standard"),
        ("openai", "gpt-5.6-luna", "future-tier"),
        ("future-provider", "gpt-5.6-luna", "standard"),
        ("openai", "deepseek-v4-flash", "standard"),
        ("deepseek", "gpt-5.6-luna", "standard"),
    ],
)
def test_unknown_or_cross_provider_prices_are_never_guessed(
    provider: str,
    model: str,
    tier: str,
) -> None:
    assert calculate_call_cost_usd(
        provider=provider,
        model=model,
        prompt_tokens=100,
        cached_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=10,
        request_input_tokens=100,
        pricing_tier=tier,
    ) is None


class _Usage:
    def __init__(self, provider: str, model: str, tokens: int) -> None:
        self.provider = provider
        self.returned_model = model
        self.prompt_tokens = tokens
        self.completion_tokens = 0
        self.cached_read_tokens = 0
        self.cache_write_tokens = 0
        self.request_input_tokens = tokens
        self.pricing_tier = "standard"


def test_mixed_provider_ledger_is_costed_per_call_before_aggregation() -> None:
    assert calculate_ledger_cost_usd(
        [
            _Usage("deepseek", "deepseek-v4-flash", 1_000_000),
            _Usage("openai", "gpt-5.6-luna", 1_000_000),
        ]
    ) == Decimal("1.1400")


def test_unpriceable_call_makes_ledger_cost_unknown() -> None:
    assert calculate_ledger_cost_usd([_Usage("openai", "unknown", 10)]) is None

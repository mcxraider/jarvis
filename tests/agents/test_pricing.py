"""Tests for DeepSeek token pricing."""

from decimal import Decimal

import pytest

from agents.agent_api.app.pricing import (
    calculate_cost_usd,
    derive_uncached_input_tokens,
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

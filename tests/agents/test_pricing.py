"""Tests for DeepSeek token pricing."""

import pytest

from agents.agent_api.app.pricing import (
    calculate_cost_microcents,
    derive_uncached_input_tokens,
)


@pytest.mark.parametrize(
    ("prompt_tokens", "cached_tokens", "output_tokens", "expected"),
    [
        (1_000_000, 1_000_000, 0, 280_000),
        (1_000_000, 0, 0, 14_000_000),
        (0, 0, 1_000_000, 28_000_000),
        (100, 40, 10, 1_131),
        (1, 1, 0, 0),
        (2, 2, 0, 1),
    ],
)
def test_flash_costs(
    prompt_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    expected: int,
) -> None:
    assert (
        calculate_cost_microcents(
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
        (1_000_000, 1_000_000, 0, 362_500),
        (1_000_000, 0, 0, 43_500_000),
        (0, 0, 1_000_000, 87_000_000),
        (100, 40, 10, 3_495),
    ],
)
def test_pro_costs(
    prompt_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    expected: int,
) -> None:
    assert (
        calculate_cost_microcents(
            "deepseek-v4-pro",
            prompt_tokens,
            cached_tokens,
            output_tokens,
        )
        == expected
    )


def test_unknown_model_has_no_cost() -> None:
    assert calculate_cost_microcents("future-model", 100, 20, 10) is None


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
        calculate_cost_microcents(
            "deepseek-v4-flash",
            prompt_tokens,
            cached_tokens,
            output_tokens,
        )


def test_uncached_tokens_are_derived_from_total_input() -> None:
    assert derive_uncached_input_tokens(150, 40) == 110

"""Token prices and the worst-case cost estimate used for budget reservations.

USD per million tokens, Anthropic first-party list prices as of 2026-10. Cache writes are
priced at 1.25x input (5-minute TTL). Vertex AI bills Claude at its own rates; for a
Vertex deployment, load these from config rather than trusting this table.
"""

from __future__ import annotations

from decimal import Decimal

from riverbend.llm.base import Usage

_M = Decimal(1_000_000)

# model -> (input, output, cache_read, cache_write)
PRICES: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {
    "claude-opus-5-5": (Decimal("4.00"), Decimal("20.00"), Decimal("0.20"), Decimal("5.00")),
    "claude-sonnet-5-5": (Decimal("2.00"), Decimal("10.00"), Decimal("0.20"), Decimal("2.50")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50"), Decimal("0.01"), Decimal("0.125")),
}

# Characters per token is ~4 for English; 2.5 over-estimates on purpose (accents, JSON,
# the provider's own wrapping of the output schema). Over-reserving is safe; under is not.
_CHARS_PER_TOKEN = Decimal("2.5")
_SCHEMA_OVERHEAD_TOKENS = 1500


def prices_for(model: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    try:
        return PRICES[model]
    except KeyError:
        # Unknown model: refuse rather than guess. A budget we cannot price is not a budget.
        raise ValueError(f"no price configured for model {model!r}") from None


def cost_of(model: str, usage: Usage) -> Decimal:
    inp, out, cache_read, cache_write = prices_for(model)
    total = (
        usage.input_tokens * inp
        + usage.output_tokens * out
        + usage.cache_read_tokens * cache_read
        + usage.cache_write_tokens * cache_write
    ) / _M
    return total.quantize(Decimal("0.000001"))


def worst_case_cost(model: str, prompt_chars: int, max_tokens: int) -> Decimal:
    inp, out, _cache_read, cache_write = prices_for(model)
    est_input = int(Decimal(prompt_chars) / _CHARS_PER_TOKEN) + _SCHEMA_OVERHEAD_TOKENS
    # Assume every input token is a cache *write* (the most expensive input case).
    worst_in = max(inp, cache_write)
    return ((est_input * worst_in + max_tokens * out) / _M).quantize(Decimal("0.000001"))

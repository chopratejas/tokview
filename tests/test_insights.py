"""Tests for the deterministic savings coach (insights.py).

All pure functions, fed a fake pricing map so the math is exact and the
tests never touch LiteLLM or the network.
"""
from __future__ import annotations

from headroom_token_view.insights import (
    cache_savings_realized,
    caching_opportunity,
    compute_insights,
    model_whatif,
    unit_prices,
)

# A fake pricing map mirroring LiteLLM's model_cost shape.
PRICING = {
    "claude-3-5-sonnet-20240620": {
        "input_cost_per_token": 3e-6,
        "output_cost_per_token": 15e-6,
        "cache_read_input_token_cost": 0.3e-6,
        "cache_creation_input_token_cost": 3.75e-6,
    },
    "claude-3-5-haiku": {
        "input_cost_per_token": 0.8e-6,
        "output_cost_per_token": 4e-6,
    },
    "gpt-4o": {
        "input_cost_per_token": 2.5e-6,
        "output_cost_per_token": 10e-6,
        # no cache_read price -> caching not "supported" for our heuristic
    },
    "gpt-4o-mini": {
        "input_cost_per_token": 0.15e-6,
        "output_cost_per_token": 0.6e-6,
    },
}


def _row(**kw):
    base = {
        "model": "anthropic/claude-3-5-sonnet-20240620",
        "input_tokens": 2000,
        "output_tokens": 100,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "cost_usd": 0.01,
        "status_code": 200,
    }
    base.update(kw)
    return base


# ---------- unit_prices ----------

def test_unit_prices_strips_provider_prefix():
    p = unit_prices("anthropic/claude-3-5-sonnet-20240620", PRICING)
    assert p["input"] == 3e-6
    assert p["cache_read"] == 0.3e-6


def test_unit_prices_unknown_model_is_zeros():
    p = unit_prices("acme/unknown-9", PRICING)
    assert p == {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_creation": 0.0}


# ---------- caching_opportunity ----------

def test_caching_opportunity_flags_repeated_uncached_large_prompts():
    rows = [_row() for _ in range(5)]  # 5 identical 2000-token uncached calls
    out = caching_opportunity(rows, PRICING)
    assert len(out) == 1
    insight = out[0]
    assert insight["type"] == "caching_opportunity"
    assert insight["affected_requests"] == 5
    # savings ~= (5-1) * 2000 * (3e-6 - 0.3e-6) = 4*2000*2.7e-6 = 0.0216
    assert insight["estimated_savings_usd"] == round(4 * 2000 * (3e-6 - 0.3e-6), 4)


def test_caching_opportunity_ignores_small_prompts():
    rows = [_row(input_tokens=100) for _ in range(5)]
    assert caching_opportunity(rows, PRICING) == []


def test_caching_opportunity_ignores_already_cached():
    rows = [_row(cache_read_tokens=2000) for _ in range(5)]
    assert caching_opportunity(rows, PRICING) == []


def test_caching_opportunity_ignores_models_without_cache_pricing():
    rows = [_row(model="openai/gpt-4o") for _ in range(5)]  # gpt-4o has no cache_read price
    assert caching_opportunity(rows, PRICING) == []


def test_caching_opportunity_needs_minimum_repeats():
    rows = [_row() for _ in range(2)]  # below _MIN_REPEATS (3)
    assert caching_opportunity(rows, PRICING) == []


def test_caching_opportunity_ignores_failures():
    rows = [_row(status_code=500) for _ in range(5)]
    assert caching_opportunity(rows, PRICING) == []


# ---------- cache_savings_realized ----------

def test_cache_savings_realized_sums_savings():
    # 2000 cache-read tokens x3 keeps total above the $0.01 surfacing floor.
    rows = [_row(cache_read_tokens=2000) for _ in range(3)]
    out = cache_savings_realized(rows, PRICING)
    assert len(out) == 1
    # each: 2000 * (3e-6 - 0.3e-6) = 0.0054; x3 = 0.0162
    assert out[0]["estimated_savings_usd"] == round(3 * 2000 * (3e-6 - 0.3e-6), 4)
    assert out[0]["severity"] == "win"


def test_cache_savings_realized_suppresses_sub_cent():
    """Savings under $0.01 are intentionally not surfaced (no nagging)."""
    rows = [_row(cache_read_tokens=500)]  # 500 * 2.7e-6 = 0.00135
    assert cache_savings_realized(rows, PRICING) == []


def test_cache_savings_realized_empty_when_no_caching():
    rows = [_row() for _ in range(3)]
    assert cache_savings_realized(rows, PRICING) == []


# ---------- model_whatif ----------

def test_model_whatif_opus_to_cheaper_sibling():
    # sonnet -> haiku what-if
    rows = [
        _row(model="anthropic/claude-3-5-sonnet-20240620", input_tokens=10000, output_tokens=2000, cost_usd=0.06)
        for _ in range(2)
    ]
    out = model_whatif(rows, PRICING)
    assert len(out) == 1
    wi = out[0]
    assert wi["type"] == "model_whatif"
    assert wi["alternative"] == "claude-3-5-haiku"
    # alt cost = 20000*0.8e-6 + 4000*4e-6 = 0.016 + 0.016 = 0.032; saving = 0.12 - 0.032
    assert wi["estimated_savings_usd"] == round(0.12 - 0.032, 4)


def test_model_whatif_no_sibling_no_insight():
    rows = [_row(model="anthropic/claude-3-5-haiku", cost_usd=0.5) for _ in range(2)]
    assert model_whatif(rows, PRICING) == []


# ---------- compute_insights (registry) ----------

def test_compute_insights_combines_and_sorts():
    rows = [_row() for _ in range(5)] + [_row(cache_read_tokens=2000) for _ in range(3)]
    out = compute_insights(rows, PRICING)
    types = {i["type"] for i in out}
    assert "caching_opportunity" in types
    assert "cache_savings_realized" in types
    # sorted descending by savings
    savings = [i["estimated_savings_usd"] for i in out]
    assert savings == sorted(savings, reverse=True)


def test_compute_insights_empty_rows():
    assert compute_insights([], PRICING) == []

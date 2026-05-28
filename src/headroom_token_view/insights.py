"""The savings coach — deterministic, local, no model calls.

Each insight generator is a pure function over the request rows plus a
pricing map (LiteLLM's local `model_cost` dict — already bundled in the
wheel, no network). It returns zero or more Insight dicts. Generators are
intentionally simple and independently testable; add more by appending to
GENERATORS.

We never call an LLM to "analyze" anything — every number here is computed
arithmetic over data we already stored. That keeps it fast, free, private,
and reproducible.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

# Public type alias for clarity.
Insight = dict[str, Any]
PricingMap = dict[str, dict[str, float]]

# A request row, as returned by db.recent_requests / session_calls.
Row = dict[str, Any]


# --- pricing helpers -------------------------------------------------------

def _strip_provider(model: str) -> str:
    """`anthropic/claude-3-5-sonnet` -> `claude-3-5-sonnet`."""
    return model.split("/", 1)[1] if "/" in model else model


def unit_prices(model: str, pricing: PricingMap) -> dict[str, float]:
    """Per-token prices for a model, tolerant of provider-prefixed names.

    Returns a dict with keys: input, output, cache_read, cache_creation.
    Missing entries are 0.0 (caller decides what that means).
    """
    entry = pricing.get(model) or pricing.get(_strip_provider(model)) or {}
    return {
        "input": float(entry.get("input_cost_per_token") or 0.0),
        "output": float(entry.get("output_cost_per_token") or 0.0),
        "cache_read": float(entry.get("cache_read_input_token_cost") or 0.0),
        "cache_creation": float(entry.get("cache_creation_input_token_cost") or 0.0),
    }


def _supports_caching(model: str, pricing: PricingMap) -> bool:
    """True when the pricing map lists a cache-read price for this model."""
    return unit_prices(model, pricing)["cache_read"] > 0.0


# --- insight generators ----------------------------------------------------

# Tunables. Conservative on purpose: we'd rather under-surface than nag.
_MIN_REPEATS = 3          # how many similar uncached calls before we flag
_MIN_INPUT_TOKENS = 1024  # only worth caching reasonably large prompts
_MIN_SAVINGS_USD = 0.01   # don't bother surfacing sub-cent opportunities


def caching_opportunity(rows: list[Row], pricing: PricingMap) -> list[Insight]:
    """Flag repeated, large, *uncached* prompts on cache-capable models.

    Heuristic: bucket successful calls by (model, rounded input size). If a
    bucket has >= _MIN_REPEATS calls, the prompt is large, the model supports
    caching, and none of the calls used caching, then caching the shared
    prefix could save roughly (repeats - 1) * input_tokens *
    (input_price - cache_read_price). We assume the *first* call pays full
    price (cache write) and subsequent calls read from cache.
    """
    buckets: dict[tuple[str, int], list[Row]] = defaultdict(list)
    for r in rows:
        if r.get("status_code", 200) >= 400:
            continue
        model = r.get("model") or ""
        in_tok = int(r.get("input_tokens") or 0)
        used_cache = (int(r.get("cache_read_tokens") or 0) + int(r.get("cache_creation_tokens") or 0)) > 0
        if in_tok < _MIN_INPUT_TOKENS or used_cache or not _supports_caching(model, pricing):
            continue
        # Round input size to a 512-token bucket so near-identical prompts group.
        bucket = (model, round(in_tok / 512) * 512)
        buckets[(bucket)].append(r)

    insights: list[Insight] = []
    for (model, approx_in), bucket_rows in buckets.items():
        if len(bucket_rows) < _MIN_REPEATS:
            continue
        prices = unit_prices(model, pricing)
        per_call_input = sum(int(r.get("input_tokens") or 0) for r in bucket_rows) / len(bucket_rows)
        savable_calls = len(bucket_rows) - 1  # first call writes the cache
        est = savable_calls * per_call_input * (prices["input"] - prices["cache_read"])
        if est < _MIN_SAVINGS_USD:
            continue
        insights.append(
            {
                "type": "caching_opportunity",
                "severity": "advice",
                "title": f"Enable prompt caching for {_strip_provider(model)}",
                "detail": (
                    f"{len(bucket_rows)} calls with ~{int(approx_in):,} input tokens ran "
                    f"without caching. Caching the shared prefix could save about "
                    f"${est:.2f}."
                ),
                "estimated_savings_usd": round(est, 4),
                "affected_requests": len(bucket_rows),
                "model": model,
            }
        )
    insights.sort(key=lambda i: i["estimated_savings_usd"], reverse=True)
    return insights


def cache_savings_realized(rows: list[Row], pricing: PricingMap) -> list[Insight]:
    """Positive reinforcement: how much caching has already saved.

    For every cache-read token, the saving is (input_price - cache_read_price)
    versus paying full input price. Summed across all rows.
    """
    total = 0.0
    cached_calls = 0
    for r in rows:
        cache_read = int(r.get("cache_read_tokens") or 0)
        if cache_read <= 0:
            continue
        prices = unit_prices(r.get("model") or "", pricing)
        if prices["input"] <= 0:
            continue
        total += cache_read * (prices["input"] - prices["cache_read"])
        cached_calls += 1

    if total < _MIN_SAVINGS_USD:
        return []
    return [
        {
            "type": "cache_savings_realized",
            "severity": "win",
            "title": "Prompt caching is saving you money",
            "detail": (
                f"Across {cached_calls} cached calls, reading from cache instead of "
                f"reprocessing input has saved about ${total:.2f} so far."
            ),
            "estimated_savings_usd": round(total, 4),
            "affected_requests": cached_calls,
            "model": None,
        }
    ]


# Registry. Each entry: (rows, pricing) -> list[Insight].
GENERATORS: tuple[Callable[[list[Row], PricingMap], list[Insight]], ...] = (
    caching_opportunity,
    cache_savings_realized,
)


def compute_insights(rows: list[Row], pricing: PricingMap) -> list[Insight]:
    """Run every generator and return the combined, savings-sorted list."""
    out: list[Insight] = []
    for gen in GENERATORS:
        try:
            out.extend(gen(rows, pricing))
        except Exception:  # one bad generator shouldn't sink the whole panel
            continue
    out.sort(key=lambda i: i.get("estimated_savings_usd", 0.0), reverse=True)
    return out


# --- session-scoped model what-if -----------------------------------------

# Minimal same-family "cheaper sibling" map for the what-if. Intentionally
# conservative — only well-known, clearly-cheaper swaps. This is informational,
# not a recommendation to blindly switch (cheaper models may be less capable).
_CHEAPER_SIBLING: dict[str, str] = {
    "claude-3-opus": "claude-3-5-sonnet",
    "claude-3-5-sonnet": "claude-3-5-haiku",
    "claude-3-7-sonnet": "claude-3-5-haiku",
    "gpt-4o": "gpt-4o-mini",
    "gpt-4-turbo": "gpt-4o-mini",
    "gemini-2.5-pro": "gemini-2.5-flash",
    "gemini-1.5-pro": "gemini-1.5-flash",
}


def _sibling_for(model: str) -> str | None:
    base = _strip_provider(model)
    for prefix, cheaper in _CHEAPER_SIBLING.items():
        if base.startswith(prefix):
            return cheaper
    return None


def model_whatif(rows: list[Row], pricing: PricingMap) -> list[Insight]:
    """Per-model 'what would this have cost on the cheaper sibling' estimate.

    Recomputes cost from the stored token counts at the sibling's unit prices.
    Used for a single session's rows (but works on any row set).
    """
    by_model: dict[str, dict[str, float]] = defaultdict(lambda: {"in": 0.0, "out": 0.0, "cost": 0.0, "n": 0.0})
    for r in rows:
        if r.get("status_code", 200) >= 400:
            continue
        m = r.get("model") or ""
        agg = by_model[m]
        agg["in"] += int(r.get("input_tokens") or 0)
        agg["out"] += int(r.get("output_tokens") or 0)
        agg["cost"] += float(r.get("cost_usd") or 0.0)
        agg["n"] += 1

    insights: list[Insight] = []
    for model, agg in by_model.items():
        sibling = _sibling_for(model)
        if not sibling:
            continue
        sp = unit_prices(sibling, pricing)
        if sp["input"] <= 0 and sp["output"] <= 0:
            continue
        alt_cost = agg["in"] * sp["input"] + agg["out"] * sp["output"]
        saving = agg["cost"] - alt_cost
        if saving < _MIN_SAVINGS_USD:
            continue
        insights.append(
            {
                "type": "model_whatif",
                "severity": "info",
                "title": f"{_strip_provider(model)} → {sibling} what-if",
                "detail": (
                    f"These {int(agg['n'])} calls cost ${agg['cost']:.2f} on "
                    f"{_strip_provider(model)}; the same tokens on {sibling} would be "
                    f"about ${alt_cost:.2f} (~${saving:.2f} less). Cheaper models may be "
                    f"less capable — informational only."
                ),
                "estimated_savings_usd": round(saving, 4),
                "affected_requests": int(agg["n"]),
                "model": model,
                "alternative": sibling,
            }
        )
    insights.sort(key=lambda i: i["estimated_savings_usd"], reverse=True)
    return insights


def load_pricing_map() -> PricingMap:
    """Return LiteLLM's local model-cost map. Empty dict if unavailable.

    LiteLLM populates `litellm.model_cost` from the bundled
    model_prices_and_context_window.json (we force-disable the remote fetch
    in server.py). Imported lazily so tests and non-proxy contexts don't pay
    the import cost.
    """
    try:
        import litellm

        return dict(getattr(litellm, "model_cost", {}) or {})
    except Exception:
        return {}

"""Tests for HtvLogger._build_row across provider streaming shapes.

These exercise the field-extraction logic against payloads that match
what LiteLLM passes into the CustomLogger after each provider's stream
completes. The point isn't to retest LiteLLM's parsing — that's verified
end-to-end in iter 9 — but to lock in HTV's interpretation of the
StandardLoggingPayload + response.usage so future LiteLLM upgrades
that reshape the payload show up as test failures, not silent data loss.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from headroom_token_view.logger import HtvLogger, _extract_usage, _provider_from_model
from tests.conftest import FakeResponse, FakeUsage, fake_now, make_kwargs


START = fake_now()
END = START + dt.timedelta(milliseconds=125)


# ---------- helpers ----------

def build_row(*, kwargs: dict[str, Any], response: Any, success: bool = True) -> dict[str, Any]:
    return HtvLogger._build_row(kwargs, response, START, END, success=success)


# ---------- Anthropic ----------

def test_anthropic_streaming_basic_usage():
    """Anthropic streaming: final usage in message_delta has input/output tokens."""
    kwargs = make_kwargs(
        model="anthropic/claude-3-5-sonnet-20240620",
        provider="anthropic",
        response_cost=0.00321,
    )
    response = FakeResponse(
        FakeUsage(
            prompt_tokens=420,
            completion_tokens=120,
            total_tokens=540,
        )
    )
    row = build_row(kwargs=kwargs, response=response)
    assert row["provider"] == "anthropic"
    assert row["model"] == "anthropic/claude-3-5-sonnet-20240620"
    assert row["input_tokens"] == 420
    assert row["output_tokens"] == 120
    assert row["cost_usd"] == pytest.approx(0.00321)
    assert row["is_stream"] == 1
    assert row["completed"] == 1
    assert row["status_code"] == 200


def test_anthropic_streaming_prompt_caching_split():
    """Anthropic cache: cache_creation_input_tokens and cache_read_input_tokens
    arrive as top-level usage fields and must land in dedicated columns so
    they can be priced at the correct tier (write vs 5-min read vs 1-hour read)."""
    kwargs = make_kwargs(
        model="anthropic/claude-3-5-sonnet-20240620",
        provider="anthropic",
        response_cost=0.00045,
    )
    response = FakeResponse(
        FakeUsage(
            prompt_tokens=12,
            completion_tokens=80,
            cache_creation_input_tokens=4096,
            cache_read_input_tokens=2048,
        )
    )
    row = build_row(kwargs=kwargs, response=response)
    assert row["cache_creation_tokens"] == 4096
    assert row["cache_read_tokens"] == 2048
    assert row["input_tokens"] == 12  # not double-counted with cache fields
    assert row["output_tokens"] == 80


def test_anthropic_extended_thinking_reasoning_tokens():
    """Claude extended thinking shows up under completion_tokens_details.reasoning_tokens."""
    kwargs = make_kwargs(
        model="anthropic/claude-3-5-sonnet-20240620",
        provider="anthropic",
        response_cost=0.0150,
    )
    response = FakeResponse(
        FakeUsage(
            prompt_tokens=200,
            completion_tokens=500,
            completion_tokens_details={"reasoning_tokens": 350},
        )
    )
    row = build_row(kwargs=kwargs, response=response)
    assert row["reasoning_tokens"] == 350
    assert row["output_tokens"] == 500


# ---------- OpenAI ----------

def test_openai_streaming_with_include_usage():
    """OpenAI streaming requires stream_options.include_usage=true; the usage
    chunk arrives last (empty choices, populated usage). HTV reads it via
    response.usage just like non-streaming."""
    kwargs = make_kwargs(
        model="openai/gpt-4o",
        provider="openai",
        response_cost=0.00750,
    )
    response = FakeResponse(
        FakeUsage(
            prompt_tokens=1024,
            completion_tokens=256,
            total_tokens=1280,
        )
    )
    row = build_row(kwargs=kwargs, response=response)
    assert row["provider"] == "openai"
    assert row["input_tokens"] == 1024
    assert row["output_tokens"] == 256
    assert row["cost_usd"] == pytest.approx(0.00750)


def test_openai_cached_tokens_discount():
    """OpenAI cached input tokens live under prompt_tokens_details.cached_tokens."""
    kwargs = make_kwargs(
        model="openai/gpt-4o",
        provider="openai",
        response_cost=0.00500,
    )
    response = FakeResponse(
        FakeUsage(
            prompt_tokens=2000,
            completion_tokens=300,
            prompt_tokens_details={"cached_tokens": 800},
        )
    )
    row = build_row(kwargs=kwargs, response=response)
    # OpenAI cached tokens are aggregated into HTV's cache_read_tokens
    # alongside Anthropic's cache_read_input_tokens.
    assert row["cache_read_tokens"] == 800
    assert row["input_tokens"] == 2000


def test_openai_o1_reasoning_tokens():
    """OpenAI o-series ('o1', 'o3') exposes reasoning tokens under
    completion_tokens_details — billed as output."""
    kwargs = make_kwargs(
        model="openai/o1-preview",
        provider="openai",
        response_cost=0.150,
    )
    response = FakeResponse(
        FakeUsage(
            prompt_tokens=500,
            completion_tokens=2000,
            completion_tokens_details={"reasoning_tokens": 1700},
        )
    )
    row = build_row(kwargs=kwargs, response=response)
    assert row["reasoning_tokens"] == 1700
    assert row["output_tokens"] == 2000


# ---------- Gemini ----------

def test_gemini_streaming_usage_metadata():
    """Gemini streaming emits usageMetadata in each chunk (populated in the
    final). After LiteLLM normalizes to the OpenAI shape we read it the
    same way."""
    kwargs = make_kwargs(
        model="gemini/gemini-2.5-pro",
        provider="gemini",
        response_cost=0.0040,
    )
    response = FakeResponse(
        FakeUsage(
            prompt_tokens=300,
            completion_tokens=180,
            total_tokens=480,
        )
    )
    row = build_row(kwargs=kwargs, response=response)
    assert row["provider"] == "gemini"
    assert row["input_tokens"] == 300
    assert row["output_tokens"] == 180


def test_gemini_context_cache():
    """Gemini context-cached tokens — same nested path as OpenAI."""
    kwargs = make_kwargs(
        model="gemini/gemini-2.5-pro",
        provider="gemini",
        response_cost=0.0025,
    )
    response = FakeResponse(
        FakeUsage(
            prompt_tokens=5000,
            completion_tokens=400,
            prompt_tokens_details={"cached_tokens": 3500},
        )
    )
    row = build_row(kwargs=kwargs, response=response)
    assert row["cache_read_tokens"] == 3500


# ---------- failure / disconnect ----------

def test_failure_event_logs_zero_cost_with_error():
    kwargs = make_kwargs(
        model="anthropic/claude-3-5-sonnet-20240620",
        provider="anthropic",
        response_cost=0.0,
        extra_slp={"status_code": 503, "error_str": "Upstream unavailable"},
    )
    kwargs["exception"] = "anthropic.APIStatusError: 503 Service Unavailable"
    row = build_row(kwargs=kwargs, response=None, success=False)
    assert row["completed"] == 0
    assert row["status_code"] == 503
    assert row["cost_usd"] == 0.0
    # kwargs.exception takes precedence over slp.error_str (more authoritative
    # — it's the raw exception object LiteLLM caught)
    assert "503 Service Unavailable" in (row["error_message"] or "")


# ---------- metadata flow ----------

def test_session_user_tags_useragent_propagate():
    kwargs = make_kwargs(
        model="openai/gpt-4o",
        provider="openai",
        session_id="claude-code-7b3a4f",
        user="alice",
        tags=["env:prod", "team:platform"],
        user_agent="claude-cli/1.4.0 python/3.13.0",
        response_cost=0.001,
    )
    response = FakeResponse(FakeUsage(prompt_tokens=100, completion_tokens=20))
    row = build_row(kwargs=kwargs, response=response)
    assert row["session_id"] == "claude-code-7b3a4f"
    assert row["user"] == "alice"
    assert '"env:prod"' in (row["tags"] or "")
    assert "claude-cli" in (row["user_agent"] or "")


# ---------- helpers / inference ----------

def test_provider_inference_from_model_when_missing():
    """If LiteLLM didn't tag the call, we infer provider from model name."""
    assert _provider_from_model("claude-3-5-sonnet-20240620") == "anthropic"
    assert _provider_from_model("anthropic/claude-3-7-sonnet") == "anthropic"
    assert _provider_from_model("gpt-4o-mini") == "openai"
    assert _provider_from_model("o1-preview") == "openai"
    assert _provider_from_model("o3-mini") == "openai"
    assert _provider_from_model("openai/gpt-4o") == "openai"
    assert _provider_from_model("gemini-2.5-pro") == "google"
    # Vertex AI is Google's hosting of Gemini — same billing namespace,
    # so HTV groups it under "google".
    assert _provider_from_model("vertex_ai/gemini-2.0") == "google"
    assert _provider_from_model("") == "unknown"
    assert _provider_from_model("some/unknown") == "some"


def test_extract_usage_handles_multiple_shapes():
    """response_obj can be a dict, a model with .usage as dict, attrs, or pydantic."""
    # dict form
    assert _extract_usage({"usage": {"prompt_tokens": 5}}) == {"prompt_tokens": 5}
    # attr-style with model_dump()
    obj = FakeResponse(FakeUsage(prompt_tokens=10, completion_tokens=2))
    assert _extract_usage(obj) == {"prompt_tokens": 10, "completion_tokens": 2}
    # None
    assert _extract_usage(None) == {}

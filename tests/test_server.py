"""Tests for tokview's LiteLLM proxy boundary."""
from __future__ import annotations

import json

from tokview.server import _rewrite_model_body, normalize_litellm_model


def test_normalize_litellm_model_prefixes_bare_claude_aliases():
    assert normalize_litellm_model("claude-opus-4-8") == "anthropic/claude-opus-4-8"
    assert (
        normalize_litellm_model("claude-3-5-sonnet-20240620")
        == "anthropic/claude-3-5-sonnet-20240620"
    )


def test_normalize_litellm_model_prefixes_openai_and_gemini_models():
    assert normalize_litellm_model("gpt-4o-mini") == "openai/gpt-4o-mini"
    assert normalize_litellm_model("o4-mini") == "openai/o4-mini"
    assert normalize_litellm_model("gemini-2.5-pro") == "gemini/gemini-2.5-pro"


def test_normalize_litellm_model_leaves_qualified_and_unknown_models():
    assert normalize_litellm_model("anthropic/claude-opus-4-8") == "anthropic/claude-opus-4-8"
    assert normalize_litellm_model("vendor-model") == "vendor-model"
    assert normalize_litellm_model(None) is None


def test_rewrite_anthropic_model_body_updates_json_model():
    body = json.dumps({"model": "claude-opus-4-8", "messages": []}).encode()

    rewritten = json.loads(_rewrite_model_body(body))

    assert rewritten["model"] == "anthropic/claude-opus-4-8"
    assert rewritten["messages"] == []


def test_rewrite_model_body_updates_openai_model():
    body = json.dumps({"model": "gpt-4o-mini", "messages": []}).encode()

    rewritten = json.loads(_rewrite_model_body(body))

    assert rewritten["model"] == "openai/gpt-4o-mini"


def test_rewrite_anthropic_model_body_ignores_invalid_json():
    body = b"{not-json"

    assert _rewrite_model_body(body) == body

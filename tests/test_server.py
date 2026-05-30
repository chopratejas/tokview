"""Tests for tokview's LiteLLM proxy boundary."""

from __future__ import annotations

import json

import pytest

from tokview.server import _litellm_provider_prefix, _rewrite_model_body, normalize_litellm_model


@pytest.mark.parametrize(
    ("model", "normalized"),
    [
        ("claude-opus-4-8", "anthropic/claude-opus-4-8"),
        ("claude-sonnet-4-5", "anthropic/claude-sonnet-4-5"),
        ("claude-3-5-sonnet-20240620", "anthropic/claude-3-5-sonnet-20240620"),
        ("gpt-4o-mini", "openai/gpt-4o-mini"),
        ("gpt-4.1-mini", "openai/gpt-4.1-mini"),
        ("o4-mini", "openai/o4-mini"),
        ("o3-mini", "openai/o3-mini"),
        ("chatgpt-4o-latest", "openai/chatgpt-4o-latest"),
        ("gemini-2.5-pro", "gemini/gemini-2.5-pro"),
        ("gemini-2.5-flash", "gemini/gemini-2.5-flash"),
        ("gemini-1.5-pro", "gemini/gemini-1.5-pro"),
        ("mistral-large-latest", "mistral/mistral-large-latest"),
        ("codestral-latest", "mistral/codestral-latest"),
        ("command-r-plus", "cohere_chat/command-r-plus"),
        ("deepseek-chat", "deepseek/deepseek-chat"),
        ("deepseek-reasoner", "deepseek/deepseek-reasoner"),
        ("grok-4", "xai/grok-4"),
        ("sonar-pro", "perplexity/sonar-pro"),
        ("llama-3.1-70b-versatile", "groq/llama-3.1-70b-versatile"),
    ],
)
def test_normalize_litellm_model_prefixes_common_bare_models(model: str, normalized: str):
    assert normalize_litellm_model(model) == normalized


def test_litellm_provider_prefix_uses_litellm_registry_when_available():
    assert _litellm_provider_prefix("gpt-4o-mini") == "openai/gpt-4o-mini"
    assert _litellm_provider_prefix("command-r-plus") == "cohere_chat/command-r-plus"


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

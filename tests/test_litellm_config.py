"""Tests for generated LiteLLM proxy config."""
from __future__ import annotations

from tokview.config import TokviewConfig
from tokview.litellm_config import build


def test_build_uses_provider_specific_wildcards():
    cfg = build(TokviewConfig())
    entries = {entry["model_name"]: entry["litellm_params"] for entry in cfg["model_list"]}

    assert entries["openai/*"]["model"] == "openai/*"
    assert entries["anthropic/*"]["model"] == "anthropic/*"
    assert entries["gemini/*"]["model"] == "gemini/*"


def test_build_reads_provider_keys_from_environment():
    cfg = build(TokviewConfig())
    entries = {entry["model_name"]: entry["litellm_params"] for entry in cfg["model_list"]}

    assert entries["openai/*"]["api_key"] == "os.environ/OPENAI_API_KEY"
    assert entries["anthropic/*"]["api_key"] == "os.environ/ANTHROPIC_API_KEY"
    assert entries["gemini/*"]["api_key"] == "os.environ/GOOGLE_API_KEY"

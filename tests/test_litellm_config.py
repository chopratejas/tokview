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
    assert entries["mistral/*"]["model"] == "mistral/*"
    assert entries["cohere_chat/*"]["model"] == "cohere_chat/*"
    assert entries["deepseek/*"]["model"] == "deepseek/*"
    assert entries["xai/*"]["model"] == "xai/*"
    assert entries["perplexity/*"]["model"] == "perplexity/*"
    assert entries["groq/*"]["model"] == "groq/*"
    assert entries["openrouter/*"]["model"] == "openrouter/*"


def test_build_reads_provider_keys_from_environment():
    cfg = build(TokviewConfig())
    entries = {entry["model_name"]: entry["litellm_params"] for entry in cfg["model_list"]}

    assert entries["openai/*"]["api_key"] == "os.environ/OPENAI_API_KEY"
    assert entries["anthropic/*"]["api_key"] == "os.environ/ANTHROPIC_API_KEY"
    assert entries["gemini/*"]["api_key"] == "os.environ/GOOGLE_API_KEY"
    assert entries["mistral/*"]["api_key"] == "os.environ/MISTRAL_API_KEY"
    assert entries["cohere_chat/*"]["api_key"] == "os.environ/COHERE_API_KEY"
    assert entries["deepseek/*"]["api_key"] == "os.environ/DEEPSEEK_API_KEY"
    assert entries["xai/*"]["api_key"] == "os.environ/XAI_API_KEY"
    assert entries["perplexity/*"]["api_key"] == "os.environ/PERPLEXITYAI_API_KEY"
    assert entries["groq/*"]["api_key"] == "os.environ/GROQ_API_KEY"
    assert entries["openrouter/*"]["api_key"] == "os.environ/OPENROUTER_API_KEY"

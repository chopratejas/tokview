"""Generate a LiteLLM proxy config.yaml from tokview config.

LiteLLM's proxy reads YAML for its model list and settings. We generate this
on every start so tokview config is the single source of truth — users edit
~/.tokview/config.yaml, not a LiteLLM file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import TokviewConfig


def build(tokview: TokviewConfig) -> dict[str, Any]:
    """Return a LiteLLM proxy config as a dict.

    Uses LiteLLM's provider-specific wildcard model groups. A single
    ``model_name: "*"`` with ``model: "*"`` is not enough for LiteLLM's
    Anthropic pass-through endpoint; requests like ``anthropic/claude-*`` are
    rejected before provider routing. These entries match LiteLLM's documented
    wildcard format and keep tokview zero-config for the common SDKs.
    """
    provider_env = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GOOGLE_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "cohere_chat": "COHERE_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "xai": "XAI_API_KEY",
        "perplexity": "PERPLEXITYAI_API_KEY",
        "groq": "GROQ_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }
    return {
        "model_list": [
            {
                "model_name": f"{provider}/*",
                "litellm_params": {
                    "model": f"{provider}/*",
                    "api_key": f"os.environ/{env_var}",
                },
            }
            for provider, env_var in provider_env.items()
        ],
        "litellm_settings": {
            "always_include_stream_usage": tokview.litellm.always_include_stream_usage,
            "drop_params": False,
            # In v1 we don't store spend in LiteLLM's tables; tokview's CustomLogger
            # (added in iter 2) owns persistence. Until then, LiteLLM proxy runs
            # in pure stateless gateway mode (no DATABASE_URL set).
        },
        "general_settings": {
            # No master_key in v1 (localhost trust boundary; see spec §6.4)
            "master_key": None,
        },
    }


def write(tokview: TokviewConfig, destination: Path) -> Path:
    """Write the generated LiteLLM config YAML to disk and return its path."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = build(tokview)
    with destination.open("w") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)
    return destination

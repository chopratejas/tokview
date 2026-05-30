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
    return {
        "model_list": [
            {
                "model_name": "openai/*",
                "litellm_params": {
                    "model": "openai/*",
                    "api_key": "os.environ/OPENAI_API_KEY",
                },
            },
            {
                "model_name": "anthropic/*",
                "litellm_params": {
                    "model": "anthropic/*",
                    "api_key": "os.environ/ANTHROPIC_API_KEY",
                },
            },
            {
                "model_name": "gemini/*",
                "litellm_params": {
                    "model": "gemini/*",
                    "api_key": "os.environ/GOOGLE_API_KEY",
                },
            },
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

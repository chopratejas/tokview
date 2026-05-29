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

    Uses a wildcard model entry so any model the client requests is routed
    by LiteLLM's built-in provider detection (Anthropic, OpenAI, Gemini, etc.).
    """
    return {
        "model_list": [
            {
                "model_name": "*",
                "litellm_params": {"model": "*"},
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

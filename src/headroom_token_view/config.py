"""HTV configuration loading.

Loads ~/.headroom-token-view/config.yaml on first start; writes a default file
if none exists. Pydantic-validated.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

DEFAULT_DIR = Path.home() / ".headroom-token-view"
DEFAULT_CONFIG_PATH = DEFAULT_DIR / "config.yaml"
DEFAULT_DB_PATH = DEFAULT_DIR / "db.sqlite"


class ProxyConfig(BaseModel):
    port: int = 4000
    bind: str = "127.0.0.1"


class DashboardConfig(BaseModel):
    port: int = 3000
    bind: str = "127.0.0.1"


class StorageConfig(BaseModel):
    path: Path = Field(default_factory=lambda: DEFAULT_DB_PATH)


class LiteLLMConfig(BaseModel):
    always_include_stream_usage: bool = True


class PricingConfig(BaseModel):
    refresh_interval_hours: int = 24
    alert_on_zero_cost: bool = True
    fallback_to_last_known_good: bool = True
    overrides: dict[str, dict[str, float]] = Field(default_factory=dict)


class RetentionConfig(BaseModel):
    days: int = 90


class CaptureConfig(BaseModel):
    prompts: bool = False
    responses: bool = False
    redact_patterns: list[str] = Field(
        default_factory=lambda: [
            r"(sk|pk)-[A-Za-z0-9]{20,}",
            r"[\w.+-]+@[\w-]+\.[\w.-]+",
        ]
    )
    max_chars_per_field: int = 8192


class HtvConfig(BaseModel):
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    litellm: LiteLLMConfig = Field(default_factory=LiteLLMConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    capture: CaptureConfig = Field(default_factory=CaptureConfig)


def load(path: Path | None = None) -> HtvConfig:
    """Load HTV config from disk. Write defaults if missing."""
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        defaults = HtvConfig()
        write(defaults, path)
        return defaults
    with path.open() as fh:
        data: dict[str, Any] = yaml.safe_load(fh) or {}
    return HtvConfig(**data)


def write(cfg: HtvConfig, path: Path | None = None) -> None:
    """Persist HTV config to disk (used for first-run defaults and `htv config set`)."""
    path = path or DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    data = cfg.model_dump(mode="json")
    with path.open("w") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)

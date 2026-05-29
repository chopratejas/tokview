"""pytest configuration: shared fixtures."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest_asyncio

from tokview.db import Database


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    yield db
    await db.close()


def make_kwargs(
    *,
    model: str,
    provider: str,
    is_stream: bool = True,
    session_id: str | None = "sess-1",
    user_agent: str = "test-client/1.0",
    user: str | None = None,
    tags: list[str] | None = None,
    request_id: str = "req-1",
    response_cost: float = 0.0,
    response_time_in_seconds: float = 0.05,
    extra_metadata: dict[str, Any] | None = None,
    extra_slp: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a LiteLLM-shaped kwargs dict for testing."""
    metadata = {
        "litellm_session_id": session_id,
        "tags": tags or [],
        "headers": {"user-agent": user_agent},
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    slp = {
        "id": request_id,
        "request_id": request_id,
        "model": model,
        "custom_llm_provider": provider,
        "response_cost": response_cost,
        "response_time_in_seconds": response_time_in_seconds,
        "stream": is_stream,
    }
    if extra_slp:
        slp.update(extra_slp)

    return {
        "model": model,
        "custom_llm_provider": provider,
        "user": user,
        "stream": is_stream,
        "litellm_params": {"metadata": metadata},
        "standard_logging_object": slp,
        "litellm_call_id": request_id,
    }


class FakeUsage:
    """Mimics LiteLLM ModelResponse.usage — attribute-style access."""

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)

    def model_dump(self) -> dict[str, Any]:
        return self.__dict__.copy()


class FakeResponse:
    """Mimics a LiteLLM ModelResponse with a .usage attribute."""

    def __init__(self, usage: FakeUsage) -> None:
        self.usage = usage


def fake_now() -> dt.datetime:
    return dt.datetime(2026, 5, 27, 12, 0, 0, tzinfo=dt.UTC)

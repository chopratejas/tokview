"""HTV's LiteLLM CustomLogger.

Hooks LiteLLM's success/failure events and writes one row per call into
HTV's SQLite. Reads the StandardLoggingPayload (`kwargs["standard_logging_object"]`)
which LiteLLM populates from the provider's actual `usage` field + the
pricing map. Cost is ground truth, not an estimate.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

from .db import Database

logger = logging.getLogger(__name__)


class HtvLogger(CustomLogger):
    """Persists each LiteLLM-handled request to HTV's SQLite."""

    def __init__(self, db: Database, pubsub: Any | None = None) -> None:
        super().__init__()
        self.db = db
        # pubsub plugs in for SSE in iter 4; keep optional for now
        self.pubsub = pubsub

    # ---- success ----------------------------------------------------------

    async def async_log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        try:
            row = self._build_row(kwargs, response_obj, start_time, end_time, success=True)
            await self.db.insert_request(row)
            if self.pubsub is not None:
                await self.pubsub.publish({"event": "spend", "row": row})
        except Exception:
            logger.exception("htv: failed to log success event")

    # Sync variant for codepaths that don't await the async hook.
    def log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        # We're inside LiteLLM which already runs an event loop; defer to async.
        # If no loop is running we silently drop — sync callers shouldn't be
        # the source of truth.
        try:
            import asyncio  # noqa: PLC0415

            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(
                    self.async_log_success_event(kwargs, response_obj, start_time, end_time)
                )
        except Exception:
            logger.exception("htv: log_success_event sync dispatch failed")

    # ---- failure ----------------------------------------------------------

    async def async_log_failure_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        try:
            row = self._build_row(kwargs, response_obj, start_time, end_time, success=False)
            await self.db.insert_request(row)
            if self.pubsub is not None:
                await self.pubsub.publish({"event": "spend", "row": row})
        except Exception:
            logger.exception("htv: failed to log failure event")

    # ---- row builder ------------------------------------------------------

    @staticmethod
    def _build_row(
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
        *,
        success: bool,
    ) -> dict[str, Any]:
        """Translate LiteLLM's StandardLoggingPayload into an HTV request row.

        Defensive on every field: LiteLLM has reshuffled this payload across
        releases. We try several common paths and fall back to safe defaults.
        """
        slp: dict[str, Any] = kwargs.get("standard_logging_object") or {}
        metadata: dict[str, Any] = kwargs.get("litellm_params", {}).get("metadata", {}) or {}

        # Identity / timestamps
        request_id = (
            slp.get("id")
            or slp.get("request_id")
            or kwargs.get("litellm_call_id")
            or f"htv-{int(time.time() * 1e6)}"
        )
        ts_ms = int(time.time() * 1000)

        # Routing
        model = slp.get("model") or kwargs.get("model") or "unknown"
        provider = (
            slp.get("custom_llm_provider")
            or kwargs.get("custom_llm_provider")
            or _provider_from_model(model)
        )

        # Cost — LiteLLM has already computed this from the provider's usage object
        cost_usd = float(slp.get("response_cost") or kwargs.get("response_cost") or 0.0)

        # Tokens — pull from response.usage (provider truth). Fall back to slp.
        usage = _extract_usage(response_obj)
        input_tokens = int(usage.get("prompt_tokens") or slp.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or slp.get("completion_tokens") or 0)

        # Caching tokens
        prompt_details = usage.get("prompt_tokens_details") or {}
        cached_tokens_openai = int(prompt_details.get("cached_tokens") or 0)
        anthropic_cache_create = int(usage.get("cache_creation_input_tokens") or 0)
        anthropic_cache_read = int(usage.get("cache_read_input_tokens") or 0)

        # Reasoning / multimodal tokens
        completion_details = usage.get("completion_tokens_details") or {}
        reasoning_tokens = int(completion_details.get("reasoning_tokens") or 0)
        image_tokens = int(prompt_details.get("image_tokens") or 0)
        audio_tokens = int(prompt_details.get("audio_tokens") or 0)

        # Metadata: session, user, tags, user-agent
        session_id = (
            metadata.get("litellm_session_id")
            or metadata.get("session_id")
            or slp.get("trace_id")
            or None
        )
        user = kwargs.get("user") or slp.get("end_user") or None
        tags = metadata.get("tags") or slp.get("request_tags") or []
        tags_json = json.dumps(tags) if tags else None

        proxy_headers = metadata.get("headers") or {}
        if isinstance(proxy_headers, dict):
            user_agent = proxy_headers.get("user-agent") or proxy_headers.get("User-Agent")
        else:
            user_agent = None

        # Streaming / completion
        is_stream = bool(slp.get("stream") or kwargs.get("stream") or False)

        # Latency
        latency_ms: int | None = None
        try:
            response_time = slp.get("response_time_in_seconds")
            if response_time is not None:
                latency_ms = int(float(response_time) * 1000)
            else:
                latency_ms = int((end_time - start_time).total_seconds() * 1000)
        except Exception:
            latency_ms = None

        error_message: str | None = None
        if not success:
            exc = kwargs.get("exception") or slp.get("error_str")
            error_message = str(exc) if exc is not None else "unknown error"

        status_code = 200 if success else int(slp.get("status_code") or 500)

        return {
            "request_id": str(request_id),
            "ts_ms": ts_ms,
            "provider": str(provider),
            "model": str(model),
            "session_id": session_id,
            "user": user,
            "tags": tags_json,
            "user_agent": user_agent,
            "team_id": None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": anthropic_cache_create,
            "cache_read_tokens": anthropic_cache_read + cached_tokens_openai,
            "cache_read_1h_tokens": 0,
            "reasoning_tokens": reasoning_tokens,
            "image_tokens": image_tokens,
            "audio_tokens": audio_tokens,
            "cost_usd": cost_usd,
            "cost_estimated": 0,
            "is_stream": int(is_stream),
            "completed": int(success),
            "latency_ms": latency_ms,
            "status_code": status_code,
            "error_message": error_message,
            "prompt_text": None,   # capture is opt-in (spec §7); not wired yet
            "response_text": None,
        }


def _provider_from_model(model: str) -> str:
    """Best-effort provider inference when LiteLLM didn't tag the call."""
    if not model:
        return "unknown"
    m = model.lower()
    if m.startswith("anthropic/") or "claude" in m:
        return "anthropic"
    if m.startswith("openai/") or m.startswith("gpt-") or m.startswith("o1") or m.startswith("o3"):
        return "openai"
    if m.startswith("gemini") or m.startswith("google/") or m.startswith("vertex_ai/"):
        return "google"
    if "/" in m:
        return m.split("/", 1)[0]
    return "unknown"


def _extract_usage(response_obj: Any) -> dict[str, Any]:
    """Return the provider 'usage' object as a dict, from whatever shape LiteLLM hands us."""
    if response_obj is None:
        return {}
    # LiteLLM ModelResponse, OpenAI SDK object, raw dict — handle all
    if isinstance(response_obj, dict):
        return response_obj.get("usage") or {}
    usage = getattr(response_obj, "usage", None)
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    # Pydantic-style
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    # Plain attributes
    return {
        attr: getattr(usage, attr)
        for attr in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "prompt_tokens_details",
            "completion_tokens_details",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
        if hasattr(usage, attr)
    }

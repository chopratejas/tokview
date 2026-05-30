"""Orchestrate the LiteLLM proxy and the tokview dashboard backend in one process.

Both services share the asyncio event loop via `uvicorn.Server` + `asyncio.gather`.
The proxy is LiteLLM's FastAPI app; the dashboard is ours. They listen on
separate ports so that exposing the dashboard never exposes the proxy and
vice-versa.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path
from typing import Any

import uvicorn
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import DEFAULT_DIR, TokviewConfig
from .db import Database
from .litellm_config import write as write_litellm_config

logger = logging.getLogger(__name__)


async def serve(tokview: TokviewConfig) -> None:
    """Run LiteLLM proxy + tokview dashboard backend in one process, until SIGINT/SIGTERM."""
    # Generate and point LiteLLM at our generated config BEFORE importing the proxy app
    litellm_config_path = DEFAULT_DIR / "litellm-config.yaml"
    write_litellm_config(tokview, litellm_config_path)
    os.environ["CONFIG_FILE_PATH"] = str(litellm_config_path)

    # SECURITY: Use the cost map bundled in the pinned LiteLLM wheel — do NOT
    # fetch model_prices_and_context_window.json from GitHub at runtime.
    # That auto-fetch is the vector for the 2026-01-27 cost-map incident.
    # tokview will add its own SHA-256-verified refresh in a later iteration
    # (per spec §8); until then, prices are pinned to the LiteLLM release.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

    # Open SQLite (creates the file + schema on first run).
    db = Database(tokview.storage.path)
    await db.open()

    # Imported lazily so the CONFIG_FILE_PATH and LITELLM_LOCAL_MODEL_COST_MAP
    # env vars are in place at module load.
    import litellm
    from litellm.proxy.proxy_server import app as litellm_app

    from .dashboard import build_app
    from .logger import TokviewLogger

    # Register the tokview CustomLogger — it fires after every LiteLLM-handled
    # call and persists the spend row + publishes to the SSE pubsub.
    from .pubsub import PubSub

    pubsub = PubSub(queue_size=200)
    tokview_logger = TokviewLogger(db=db, pubsub=pubsub)
    litellm.callbacks = [tokview_logger]

    dashboard_app = build_app(db=db, pubsub=pubsub)
    proxy_app = ModelPrefixMiddleware(litellm_app)

    proxy_cfg = uvicorn.Config(
        proxy_app,
        host=tokview.proxy.bind,
        port=tokview.proxy.port,
        log_level="info",
        access_log=False,
    )
    dash_cfg = uvicorn.Config(
        dashboard_app,
        host=tokview.dashboard.bind,
        port=tokview.dashboard.port,
        log_level="info",
        access_log=False,
    )

    proxy_server = uvicorn.Server(proxy_cfg)
    dash_server = uvicorn.Server(dash_cfg)

    loop = asyncio.get_running_loop()

    def request_shutdown() -> None:
        logger.info("shutdown requested; stopping proxy and dashboard")
        proxy_server.should_exit = True
        dash_server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            # Windows fallback — uvicorn handles SIGINT itself
            pass

    try:
        await asyncio.gather(
            proxy_server.serve(),
            dash_server.serve(),
        )
    finally:
        await db.close()


def litellm_config_path() -> Path:
    """Where the generated LiteLLM config lives."""
    return DEFAULT_DIR / "litellm-config.yaml"


def normalize_litellm_model(model: Any) -> Any:
    """Provider-qualify common bare SDK model names for LiteLLM.

    SDKs send provider-native model names like ``claude-opus-4-8`` and
    ``gpt-4o-mini``. LiteLLM's proxy wildcard groups route reliably when those
    are provider-qualified as ``anthropic/...`` or ``openai/...``.
    """
    if not isinstance(model, str):
        return model
    if "/" in model:
        return model
    m = model.lower()
    if m.startswith("claude"):
        return f"anthropic/{model}"
    if m.startswith(("gpt-", "o1", "o3", "o4", "o5", "chatgpt-")):
        return f"openai/{model}"
    if m.startswith("gemini"):
        return f"gemini/{model}"
    return model


normalize_anthropic_model = normalize_litellm_model


class ModelPrefixMiddleware:
    """Rewrite bare provider model names in JSON requests before LiteLLM routes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin1").lower(): v.decode("latin1")
            for k, v in scope.get("headers", [])
        }
        content_type = headers.get("content-type", "")
        if "application/json" not in content_type:
            await self.app(scope, receive, send)
            return

        body = b""
        more_body = True
        while more_body:
            message = await receive()
            body += message.get("body", b"")
            more_body = bool(message.get("more_body", False))

        rewritten = _rewrite_model_body(body)
        if rewritten != body:
            scope = dict(scope)
            headers_out: list[tuple[bytes, bytes]] = []
            saw_content_length = False
            for key, value in scope.get("headers", []):
                if key.lower() == b"content-length":
                    headers_out.append((key, str(len(rewritten)).encode("ascii")))
                    saw_content_length = True
                else:
                    headers_out.append((key, value))
            if not saw_content_length:
                headers_out.append((b"content-length", str(len(rewritten)).encode("ascii")))
            scope["headers"] = headers_out

        async def replay() -> Message:
            return {"type": "http.request", "body": rewritten, "more_body": False}

        await self.app(scope, replay, send)


AnthropicModelPrefixMiddleware = ModelPrefixMiddleware


def _rewrite_model_body(body: bytes) -> bytes:
    if not body:
        return body
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body
    if not isinstance(payload, dict) or "model" not in payload:
        return body
    normalized = normalize_litellm_model(payload.get("model"))
    if normalized == payload.get("model"):
        return body
    payload["model"] = normalized
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


_rewrite_anthropic_model_body = _rewrite_model_body

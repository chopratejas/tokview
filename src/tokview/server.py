"""Orchestrate the LiteLLM proxy and the tokview dashboard backend in one process.

Both services share the asyncio event loop via `uvicorn.Server` + `asyncio.gather`.
The proxy is LiteLLM's FastAPI app; the dashboard is ours. They listen on
separate ports so that exposing the dashboard never exposes the proxy and
vice-versa.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

import uvicorn
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import DEFAULT_DIR, TokviewConfig
from .db import Database
from .litellm_config import write as write_litellm_config
from .tools import parse_completed_tool_calls

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
    proxy_app = NativeSubscriptionMiddleware(
        ModelPrefixMiddleware(litellm_app), db=db, pubsub=pubsub
    )

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
    are provider-qualified. For less common providers, ask LiteLLM's own model
    registry; if it can identify the provider, use that provider prefix.
    """
    if not isinstance(model, str):
        return model
    if "/" in model:
        return model
    m = model.lower()

    # Prefer tokview's zero-config provider groups for SDK-native model names.
    # LiteLLM currently resolves bare Gemini models to vertex_ai in some
    # releases, but the common local API-key setup is gemini/ + GOOGLE_API_KEY.
    if m.startswith("claude"):
        return f"anthropic/{model}"
    if m.startswith(("gpt-", "o1", "o3", "o4", "o5", "chatgpt-")):
        return f"openai/{model}"
    if m.startswith("gemini"):
        return f"gemini/{model}"
    if m.startswith(("mistral-", "codestral-")):
        return f"mistral/{model}"
    if m.startswith("command-"):
        return f"cohere_chat/{model}"
    if m.startswith("deepseek-"):
        return f"deepseek/{model}"
    if m.startswith("grok-"):
        return f"xai/{model}"
    if m.startswith("sonar-"):
        return f"perplexity/{model}"
    if m.startswith("llama-") and m.endswith("-versatile"):
        return f"groq/{model}"

    detected = _litellm_provider_prefix(model)
    if detected is not None:
        return detected
    return model


normalize_anthropic_model = normalize_litellm_model


CHATGPT_CODEX_RESPONSES_HTTP = "https://chatgpt.com/backend-api/codex/responses"
ANTHROPIC_MESSAGES_HTTP = "https://api.anthropic.com/v1/messages"
MAX_PROXY_BODY_BYTES = 32 * 1024 * 1024
MAX_PROXY_CAPTURE_BYTES = 8 * 1024 * 1024


class ProxyPayloadTooLarge(Exception):
    """Raised when a local proxy request exceeds tokview's forwarding limit."""


class _CappedBodyCapture:
    """Keep enough upstream bytes for accounting without unbounded memory growth."""

    def __init__(self, max_bytes: int = MAX_PROXY_CAPTURE_BYTES) -> None:
        self.max_bytes = max(0, max_bytes)
        self._body = bytearray()
        self.truncated = False

    def add(self, chunk: bytes) -> None:
        if not chunk:
            return
        remaining = self.max_bytes - len(self._body)
        if remaining > 0:
            self._body.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.truncated = True

    @property
    def body(self) -> bytes:
        return bytes(self._body)


async def _read_limited_body(
    receive: Receive, *, max_bytes: int = MAX_PROXY_BODY_BYTES
) -> bytes:
    """Read one ASGI HTTP request body, rejecting it once it exceeds max_bytes."""
    body = bytearray()
    more_body = True
    while more_body:
        message = await receive()
        chunk = message.get("body", b"")
        if chunk:
            if len(body) + len(chunk) > max_bytes:
                raise ProxyPayloadTooLarge
            body.extend(chunk)
        more_body = bool(message.get("more_body", False))
    return bytes(body)


async def _send_json_error(send: Send, status_code: int, message: str) -> None:
    body = json.dumps({"error": {"message": message}}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


def _httpx_timeout() -> Any:
    import httpx

    return httpx.Timeout(
        timeout=300.0,
        connect=30.0,
        read=300.0,
        write=30.0,
        pool=30.0,
    )


class NativeSubscriptionMiddleware:
    """Native subscription transport adapters before LiteLLM.

    LiteLLM still handles normal provider API-key traffic. Subscription CLIs use
    provider-native OAuth/backend transports that LiteLLM cannot execute, so
    tokview forwards those byte-for-byte and then normalizes usage/cost/tool rows
    with LiteLLM's local tokenizer and pricing helpers.
    """

    def __init__(self, app: ASGIApp, *, db: Database, pubsub: Any | None = None) -> None:
        self.app = app
        self.db = db
        self.pubsub = pubsub

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path")
        if path == "/v1/responses":
            if scope["type"] == "http":
                await self._handle_openai_responses_http(scope, receive, send)
                return
            if scope["type"] == "websocket":
                await self._handle_openai_responses_websocket(scope, receive, send)
                return
        if path == "/v1/messages" and scope["type"] == "http":
            await self._handle_anthropic_messages_http(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def _handle_openai_responses_http(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        headers = _scope_headers(scope)
        upstream_headers, is_chatgpt_auth = _resolve_codex_routing_headers(headers)
        if not is_chatgpt_auth:
            await self.app(scope, receive, send)
            return

        try:
            body = await _read_limited_body(receive)
        except ProxyPayloadTooLarge:
            await _send_json_error(send, 413, "request body too large")
            return

        start_ms = int(time.time() * 1000)
        request_id = f"tokview-codex-{start_ms}-{id(scope)}"
        query = scope.get("query_string") or b""
        url = CHATGPT_CODEX_RESPONSES_HTTP
        if query:
            url += "?" + query.decode("latin1")

        status_code, response_body, error_message = await _stream_post_responses(
            url=url, headers=upstream_headers, body=body, send=send
        )
        end_ms = int(time.time() * 1000)
        await self._record_response(
            request_id=request_id,
            request_body=body,
            response_body=response_body,
            request_headers=headers,
            status_code=status_code,
            start_ms=start_ms,
            end_ms=end_ms,
            error_message=error_message,
        )

    async def _handle_openai_responses_websocket(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        headers = _scope_headers(scope)
        upstream_headers, is_chatgpt_auth = _resolve_codex_routing_headers(headers)
        if not is_chatgpt_auth:
            await self.app(scope, receive, send)
            return

        message = await receive()
        if message.get("type") != "websocket.connect":
            return
        subprotocols = scope.get("subprotocols") or []
        await send(
            {
                "type": "websocket.accept",
                "subprotocol": subprotocols[0] if subprotocols else None,
                "headers": [],
            }
        )

        start_ms = int(time.time() * 1000)
        request_id = f"tokview-codex-ws-{start_ms}-{id(scope)}"
        response_body = b""
        request_body = b"{}"
        status_code = 502
        error_message: str | None = None
        try:
            first = await asyncio.wait_for(receive(), timeout=60)
            if first.get("type") == "websocket.disconnect":
                return
            text = first.get("text")
            if text is None and first.get("bytes") is not None:
                text = first["bytes"].decode("utf-8", errors="replace")
            request_body = _ws_response_create_body(text or "{}")
            status_code, _, response_body, error_message = await _post_responses(
                url=CHATGPT_CODEX_RESPONSES_HTTP + "?stream=true",
                headers=upstream_headers,
                body=request_body,
            )
            if _looks_like_sse(response_body):
                for event in _sse_json_events(response_body):
                    await send(
                        {"type": "websocket.send", "text": json.dumps(event, separators=(",", ":"))}
                    )
            else:
                decoded = response_body.decode("utf-8", errors="replace")
                await send({"type": "websocket.send", "text": decoded})
        except ProxyPayloadTooLarge:
            status_code = 413
            error_message = "request body too large"
            await send(
                {
                    "type": "websocket.send",
                    "text": json.dumps({"type": "error", "error": {"message": error_message}}),
                }
            )
        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"
            await send(
                {
                    "type": "websocket.send",
                    "text": json.dumps({"type": "error", "error": {"message": error_message}}),
                }
            )
        finally:
            end_ms = int(time.time() * 1000)
            await self._record_response(
                request_id=request_id,
                request_body=request_body,
                response_body=response_body,
                request_headers=headers,
                status_code=status_code,
                start_ms=start_ms,
                end_ms=end_ms,
                error_message=error_message,
            )
            await send({"type": "websocket.close", "code": 1000})

    async def _handle_anthropic_messages_http(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        headers = _scope_headers(scope)
        if not _is_anthropic_native_auth(headers):
            await self.app(scope, receive, send)
            return

        try:
            body = await _read_limited_body(receive)
        except ProxyPayloadTooLarge:
            await _send_json_error(send, 413, "request body too large")
            return

        request_json = _loads_json(body) or {}
        start_ms = int(time.time() * 1000)
        request_id = f"tokview-anthropic-{start_ms}-{id(scope)}"
        status_code, response_body, error_message = await _stream_post_responses(
            url=ANTHROPIC_MESSAGES_HTTP, headers=headers, body=body, send=send
        )
        end_ms = int(time.time() * 1000)
        await self._record_anthropic_message(
            request_id=request_id,
            request_json=request_json,
            response_body=response_body,
            request_headers=headers,
            status_code=status_code,
            start_ms=start_ms,
            end_ms=end_ms,
            error_message=error_message,
        )

    async def _record_anthropic_message(
        self,
        *,
        request_id: str,
        request_json: dict[str, Any],
        response_body: bytes,
        request_headers: dict[str, str],
        status_code: int,
        start_ms: int,
        end_ms: int,
        error_message: str | None,
    ) -> None:
        response_json = _loads_json(response_body)
        usage = _anthropic_usage(response_json, response_body)
        model = str(request_json.get("model") or _responses_model({}, response_json) or "unknown")
        metadata = (
            request_json.get("metadata") if isinstance(request_json.get("metadata"), dict) else {}
        )
        session_id = _responses_session_id(request_json, response_json, metadata)
        if not session_id:
            session_id = _fallback_session_id("anthropic", model, request_headers, request_json)
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cache_creation_tokens = int(usage.get("cache_creation_tokens") or 0)
        cache_read_tokens = int(usage.get("cache_read_tokens") or 0)
        completed = 200 <= status_code < 400
        row = {
            "request_id": request_id,
            "ts_ms": end_ms,
            "provider": "anthropic",
            "model": model,
            "session_id": session_id,
            "user": None,
            "tags": json.dumps(metadata.get("tags")) if metadata.get("tags") else None,
            "user_agent": request_headers.get("user-agent"),
            "team_id": None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_read_1h_tokens": 0,
            "reasoning_tokens": 0,
            "image_tokens": 0,
            "audio_tokens": 0,
            "cost_usd": _anthropic_cost_usd(model, usage),
            "cost_estimated": 1,
            "is_stream": int(bool(request_json.get("stream")) or _looks_like_sse(response_body)),
            "completed": int(completed),
            "latency_ms": max(end_ms - start_ms, 0),
            "start_ms": start_ms,
            "ttft_ms": None,
            "status_code": status_code,
            "error_message": error_message if not completed else None,
            "prompt_text": None,
            "response_text": None,
        }
        await self.db.insert_request(row)
        if self.pubsub is not None:
            await self.pubsub.publish({"event": "spend", "row": row})
        await self._record_tool_rows(request_json.get("messages"), row)

    async def _record_tool_rows(self, messages: Any, row: dict[str, Any]) -> None:
        if not isinstance(messages, list):
            return
        parsed = parse_completed_tool_calls(messages, row["model"], _litellm_count_tokens)
        if not parsed:
            return
        await self.db.insert_tool_calls(
            [
                {
                    "tool_call_id": p["id"],
                    "request_id": row["request_id"],
                    "session_id": row.get("session_id"),
                    "ts_ms": row["ts_ms"],
                    "provider": row.get("provider"),
                    "model": row.get("model"),
                    "tool_name": p["name"],
                    "arg_tokens": p["arg_tokens"],
                    "result_tokens": p["result_tokens"],
                    "total_tokens": p["arg_tokens"] + p["result_tokens"],
                }
                for p in parsed
            ]
        )

    async def _record_response(
        self,
        *,
        request_id: str,
        request_body: bytes,
        response_body: bytes,
        request_headers: dict[str, str],
        status_code: int,
        start_ms: int,
        end_ms: int,
        error_message: str | None,
    ) -> None:
        request_json = _loads_json(request_body) or {}
        response_json = _loads_json(response_body)
        completed = 200 <= status_code < 400
        usage = _responses_usage(response_json, response_body)
        model = _responses_model(request_json, response_json)
        metadata = (
            request_json.get("metadata") if isinstance(request_json.get("metadata"), dict) else {}
        )
        session_id = _responses_session_id(request_json, response_json, metadata)
        if not session_id:
            session_id = _fallback_session_id(
                "openai-chatgpt", model, request_headers, request_json
            )
        cached_tokens = int(usage.get("cached_tokens") or 0)
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cost_usd = _responses_cost_usd(model, usage)
        row = {
            "request_id": request_id,
            "ts_ms": end_ms,
            "provider": "openai-chatgpt",
            "model": model,
            "session_id": session_id,
            "user": request_json.get("user") if isinstance(request_json, dict) else None,
            "tags": json.dumps(metadata.get("tags")) if metadata.get("tags") else None,
            "user_agent": request_headers.get("user-agent"),
            "team_id": None,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": max(input_tokens - cached_tokens, 0),
            "cache_read_tokens": cached_tokens,
            "cache_read_1h_tokens": 0,
            "reasoning_tokens": int(usage.get("reasoning_tokens") or 0),
            "image_tokens": 0,
            "audio_tokens": 0,
            "cost_usd": cost_usd,
            "cost_estimated": 1,
            "is_stream": int(bool(request_json.get("stream")) or _looks_like_sse(response_body)),
            "completed": int(completed),
            "latency_ms": max(end_ms - start_ms, 0),
            "start_ms": start_ms,
            "ttft_ms": None,
            "status_code": status_code,
            "error_message": error_message if not completed else None,
            "prompt_text": None,
            "response_text": None,
        }
        await self.db.insert_request(row)
        if self.pubsub is not None:
            await self.pubsub.publish({"event": "spend", "row": row})
        await self._record_tool_rows(_responses_messages(request_json), row)


def _fallback_session_id(
    provider: str, model: str, request_headers: dict[str, str], request_json: dict[str, Any]
) -> str:
    client = _client_label(request_headers)
    material = _session_seed_material(request_json)
    if not material:
        material = {"client": client, "provider": provider, "model": model}
    digest = hashlib.sha1(
        json.dumps(material, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    return f"{client}-{provider}-{digest}"


def _session_seed_material(request_json: dict[str, Any]) -> Any:
    metadata = request_json.get("metadata")
    if isinstance(metadata, dict):
        for key in ("litellm_session_id", "session_id", "thread_id", "conversation_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return {key: value.strip()}
    for key in ("input", "messages"):
        value = request_json.get(key)
        if isinstance(value, list) and value:
            return {key: _session_seed_items(value)}
        if isinstance(value, str) and value.strip():
            return {key: value[:4000]}
    instructions = request_json.get("instructions") or request_json.get("system")
    if isinstance(instructions, str) and instructions.strip():
        return {"instructions": instructions[:4000]}
    return None


def _session_seed_items(items: list[Any]) -> list[Any]:
    seed: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            seed.append(item)
        else:
            item_type = item.get("type")
            role = item.get("role")
            # Tool outputs grow every turn and would make the digest drift; the
            # first user/developer/system content is the session anchor.
            if item_type in {"function_call_output", "tool_result"} or role == "tool":
                continue
            seed.append(
                {k: item.get(k) for k in ("type", "role", "content", "text", "name") if k in item}
            )
        if len(seed) >= 5:
            break
    return seed


def _client_label(headers: dict[str, str]) -> str:
    ua = (headers.get("user-agent") or "").lower()
    if "codex" in ua:
        return "codex"
    if "claude" in ua:
        return "claude"
    if "cursor" in ua:
        return "cursor"
    if "copilot" in ua:
        return "copilot"
    return "session"


def _scope_headers(scope: Scope) -> dict[str, str]:
    return {
        key.decode("latin1").lower(): value.decode("latin1")
        for key, value in scope.get("headers", [])
    }


def _loads_json(body: bytes) -> dict[str, Any] | None:
    if not body:
        return None
    try:
        value = json.loads(body)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _is_anthropic_native_auth(headers: dict[str, str]) -> bool:
    ua = (headers.get("user-agent") or "").lower()
    auth = headers.get("authorization") or ""
    return bool(
        "claude-code/" in ua
        or "claude-cli/" in ua
        or "anthropic-cli/" in ua
        or headers.get("anthropic-version")
        or headers.get("x-api-key")
        or auth.startswith("Bearer sk-ant-")
    )


def _litellm_count_tokens(model: str, text: str) -> int:
    try:
        from litellm import token_counter

        return int(token_counter(model=normalize_litellm_model(model), text=text))
    except Exception:
        return max(1, len(text) // 4) if text else 0


def _decode_openai_bearer_payload(headers: dict[str, str]) -> dict[str, Any] | None:
    auth = headers.get("authorization") or ""
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or token.count(".") < 2:
        return None
    payload = token.split(".", 2)[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _resolve_codex_routing_headers(headers: dict[str, str]) -> tuple[dict[str, str], bool]:
    resolved = dict(headers)
    if "chatgpt-account-id" in resolved:
        return resolved, True
    payload = _decode_openai_bearer_payload(resolved)
    auth_claims = payload.get("https://api.openai.com/auth") if isinstance(payload, dict) else None
    account_id = auth_claims.get("chatgpt_account_id") if isinstance(auth_claims, dict) else None
    if isinstance(account_id, str) and account_id.strip():
        resolved["chatgpt-account-id"] = account_id.strip()
        return resolved, True
    return resolved, False


async def _stream_post_responses(
    *, url: str, headers: dict[str, str], body: bytes, send: Send
) -> tuple[int, bytes, str | None]:
    status_code = 502
    capture = _CappedBodyCapture()
    error_message: str | None = None
    response_started = False
    try:
        import httpx

        async with (
            httpx.AsyncClient(timeout=_httpx_timeout()) as client,
            client.stream(
                "POST",
                url,
                headers=_upstream_headers(headers),
                content=body,
            ) as response,
        ):
            status_code = response.status_code
            await send(
                {
                    "type": "http.response.start",
                    "status": status_code,
                    "headers": _response_headers(response.headers),
                }
            )
            response_started = True
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                capture.add(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        if not response_started:
            capture = _CappedBodyCapture()
            capture.add(json.dumps({"error": {"message": error_message}}).encode("utf-8"))
            await send(
                {
                    "type": "http.response.start",
                    "status": status_code,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": capture.body, "more_body": False})
    return status_code, capture.body, error_message


async def _post_responses(
    *, url: str, headers: dict[str, str], body: bytes
) -> tuple[int, list[tuple[bytes, bytes]], bytes, str | None]:
    status_code = 502
    response_headers: list[tuple[bytes, bytes]] = []
    response_body = b""
    error_message: str | None = None
    try:
        import httpx

        capture = _CappedBodyCapture()
        async with httpx.AsyncClient(timeout=_httpx_timeout()) as client, client.stream(
            "POST",
            url,
            headers=_upstream_headers(headers),
            content=body,
        ) as response:
            status_code = response.status_code
            response_headers = _response_headers(response.headers)
            async for chunk in response.aiter_bytes():
                capture.add(chunk)
        response_body = capture.body
    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        response_body = json.dumps({"error": {"message": error_message}}).encode("utf-8")
        response_headers = [(b"content-type", b"application/json")]
    return status_code, response_headers, response_body, error_message


def _normalize_ws_response_create(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    response = parsed.get("response")
    if isinstance(response, dict):
        body = dict(response)
    else:
        body = dict(parsed)
        if body.get("type") == "response.create":
            body.pop("type", None)
    body["stream"] = True
    return body


def _ws_response_create_body(raw: str, *, max_bytes: int = MAX_PROXY_BODY_BYTES) -> bytes:
    body = json.dumps(_normalize_ws_response_create(raw)).encode("utf-8")
    if len(body) > max_bytes:
        raise ProxyPayloadTooLarge
    return body


def _upstream_headers(headers: dict[str, str]) -> dict[str, str]:
    skip = {
        "host",
        "content-length",
        "connection",
        "accept-encoding",
        "transfer-encoding",
        "upgrade",
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-extensions",
        "sec-websocket-accept",
        "sec-websocket-protocol",
    }
    out = {k: v for k, v in headers.items() if k.lower() not in skip}
    out.setdefault("content-type", "application/json")
    return out


def _response_headers(headers: Any) -> list[tuple[bytes, bytes]]:
    skip = {"connection", "transfer-encoding", "content-encoding", "content-length"}
    out = []
    for key, value in headers.items():
        if key.lower() in skip:
            continue
        out.append((key.lower().encode("latin1"), str(value).encode("latin1")))
    return out


def _anthropic_usage(response_json: dict[str, Any] | None, response_body: bytes) -> dict[str, int]:
    candidates: list[dict[str, Any]] = []
    if isinstance(response_json, dict) and isinstance(response_json.get("usage"), dict):
        candidates.append(response_json["usage"])
    if _looks_like_sse(response_body):
        for event in _sse_json_events(response_body):
            if isinstance(event.get("usage"), dict):
                candidates.append(event["usage"])
            message = event.get("message")
            if isinstance(message, dict) and isinstance(message.get("usage"), dict):
                candidates.append(message["usage"])
    merged: dict[str, Any] = {}
    for candidate in candidates:
        for key, value in candidate.items():
            if value is not None:
                merged[key] = value

    def as_int(value: Any) -> int:
        try:
            return max(int(value), 0)
        except Exception:
            return 0

    return {
        "input_tokens": as_int(merged.get("input_tokens")),
        "output_tokens": as_int(merged.get("output_tokens")),
        "cache_creation_tokens": as_int(merged.get("cache_creation_input_tokens")),
        "cache_read_tokens": as_int(merged.get("cache_read_input_tokens")),
    }


def _anthropic_cost_usd(model: str, usage: dict[str, int]) -> float:
    try:
        import litellm

        response = {
            "usage": {
                "prompt_tokens": int(usage.get("input_tokens") or 0),
                "completion_tokens": int(usage.get("output_tokens") or 0),
                "cache_creation_input_tokens": int(usage.get("cache_creation_tokens") or 0),
                "cache_read_input_tokens": int(usage.get("cache_read_tokens") or 0),
            }
        }
        return float(
            litellm.completion_cost(
                completion_response=response,
                model=normalize_litellm_model(model),
                call_type="anthropic_messages",
            )
            or 0.0
        )
    except Exception:
        return 0.0


def _responses_messages(request_json: dict[str, Any]) -> list[dict[str, Any]] | None:
    messages = request_json.get("messages")
    if isinstance(messages, list):
        return messages
    # Responses API requests often carry prior conversation items in `input`.
    # Convert the common tool-call/result shapes into chat-like messages so the
    # existing parser can produce a unified tool breakdown.
    items = request_json.get("input")
    if not isinstance(items, list):
        return None
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            call_id = item.get("call_id") or item.get("id")
            out.append(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": item.get("name") or "unknown",
                                "arguments": item.get("arguments") or "",
                            },
                        }
                    ],
                }
            )
        elif item_type == "function_call_output":
            call_id = item.get("call_id") or item.get("id")
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": item.get("output") or item.get("content") or "",
                }
            )
    return out or None


def _responses_usage(response_json: dict[str, Any] | None, response_body: bytes) -> dict[str, int]:
    candidates: list[dict[str, Any]] = []
    if isinstance(response_json, dict):
        if isinstance(response_json.get("usage"), dict):
            candidates.append(response_json["usage"])
        response = response_json.get("response")
        if isinstance(response, dict) and isinstance(response.get("usage"), dict):
            candidates.append(response["usage"])
    if _looks_like_sse(response_body):
        for event in _sse_json_events(response_body):
            if isinstance(event.get("usage"), dict):
                candidates.append(event["usage"])
            response = event.get("response")
            if isinstance(response, dict) and isinstance(response.get("usage"), dict):
                candidates.append(response["usage"])
    usage = candidates[-1] if candidates else {}

    def as_int(value: Any) -> int:
        try:
            return max(int(value), 0)
        except Exception:
            return 0

    input_details = usage.get("input_tokens_details") if isinstance(usage, dict) else None
    output_details = usage.get("output_tokens_details") if isinstance(usage, dict) else None
    return {
        "input_tokens": as_int(usage.get("input_tokens") if isinstance(usage, dict) else 0),
        "output_tokens": as_int(usage.get("output_tokens") if isinstance(usage, dict) else 0),
        "cached_tokens": as_int(
            input_details.get("cached_tokens") if isinstance(input_details, dict) else 0
        ),
        "reasoning_tokens": as_int(
            output_details.get("reasoning_tokens") if isinstance(output_details, dict) else 0
        ),
    }


def _responses_cost_usd(model: str, usage: dict[str, int]) -> float:
    try:
        import litellm

        response = {
            "usage": {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "input_tokens_details": {
                    "cached_tokens": int(usage.get("cached_tokens") or 0),
                },
                "output_tokens_details": {
                    "reasoning_tokens": int(usage.get("reasoning_tokens") or 0),
                },
            }
        }
        return float(
            litellm.completion_cost(
                completion_response=response,
                model=normalize_litellm_model(model),
                call_type="responses",
            )
            or 0.0
        )
    except Exception:
        return 0.0


def _responses_model(request_json: dict[str, Any], response_json: dict[str, Any] | None) -> str:
    model = request_json.get("model")
    if not model and isinstance(response_json, dict):
        model = response_json.get("model")
        response = response_json.get("response")
        if not model and isinstance(response, dict):
            model = response.get("model")
    return str(model or "unknown")


def _responses_session_id(
    request_json: dict[str, Any], response_json: dict[str, Any] | None, metadata: dict[str, Any]
) -> str | None:
    for key in ("litellm_session_id", "session_id", "thread_id", "conversation_id"):
        value = metadata.get(key) or request_json.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    previous = request_json.get("previous_response_id")
    if isinstance(previous, str) and previous.strip():
        return previous.strip()
    if isinstance(response_json, dict):
        value = response_json.get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _looks_like_sse(body: bytes) -> bool:
    stripped = body.lstrip()
    return stripped.startswith(b"data:") or stripped.startswith(b"event:")


def _sse_json_events(body: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            continue
        event = _loads_json(data)
        if event is not None:
            events.append(event)
    return events


class ModelPrefixMiddleware:
    """Rewrite bare provider model names in JSON requests before LiteLLM routes."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int = MAX_PROXY_BODY_BYTES) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])
        }
        content_type = headers.get("content-type", "")
        if "application/json" not in content_type:
            await self.app(scope, receive, send)
            return

        try:
            body = await _read_limited_body(receive, max_bytes=self.max_body_bytes)
        except ProxyPayloadTooLarge:
            await _send_json_error(send, 413, "request body too large")
            return

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


def _litellm_provider_prefix(model: str) -> str | None:
    try:
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        import litellm

        resolved_model, provider, _, _ = litellm.get_llm_provider(model=model)
    except Exception:
        return None
    if not provider or not isinstance(provider, str):
        return None
    provider = "gemini" if provider == "google" else provider
    if provider == "openai" and model.startswith("openai/"):
        return model
    if "/" in str(resolved_model):
        return str(resolved_model)
    return f"{provider}/{resolved_model or model}"

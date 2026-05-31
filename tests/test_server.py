"""Tests for tokview's LiteLLM proxy boundary."""

from __future__ import annotations

import json

import pytest

from tokview.server import (
    ModelPrefixMiddleware,
    ProxyPayloadTooLarge,
    _anthropic_cost_usd,
    _anthropic_usage,
    _CappedBodyCapture,
    _client_label,
    _fallback_session_id,
    _httpx_timeout,
    _is_anthropic_native_auth,
    _litellm_provider_prefix,
    _normalize_ws_response_create,
    _read_limited_body,
    _resolve_codex_routing_headers,
    _responses_cost_usd,
    _responses_usage,
    _rewrite_model_body,
    _ws_response_create_body,
    normalize_litellm_model,
)


@pytest.mark.parametrize(
    ("model", "normalized"),
    [
        ("claude-opus-4-8", "anthropic/claude-opus-4-8"),
        ("claude-sonnet-4-5", "anthropic/claude-sonnet-4-5"),
        ("claude-3-5-sonnet-20240620", "anthropic/claude-3-5-sonnet-20240620"),
        ("gpt-4o-mini", "openai/gpt-4o-mini"),
        ("gpt-4.1-mini", "openai/gpt-4.1-mini"),
        ("o4-mini", "openai/o4-mini"),
        ("o3-mini", "openai/o3-mini"),
        ("chatgpt-4o-latest", "openai/chatgpt-4o-latest"),
        ("gemini-2.5-pro", "gemini/gemini-2.5-pro"),
        ("gemini-2.5-flash", "gemini/gemini-2.5-flash"),
        ("gemini-1.5-pro", "gemini/gemini-1.5-pro"),
        ("mistral-large-latest", "mistral/mistral-large-latest"),
        ("codestral-latest", "mistral/codestral-latest"),
        ("command-r-plus", "cohere_chat/command-r-plus"),
        ("deepseek-chat", "deepseek/deepseek-chat"),
        ("deepseek-reasoner", "deepseek/deepseek-reasoner"),
        ("grok-4", "xai/grok-4"),
        ("sonar-pro", "perplexity/sonar-pro"),
        ("llama-3.1-70b-versatile", "groq/llama-3.1-70b-versatile"),
    ],
)
def test_normalize_litellm_model_prefixes_common_bare_models(model: str, normalized: str):
    assert normalize_litellm_model(model) == normalized


def test_litellm_provider_prefix_uses_litellm_registry_when_available():
    assert _litellm_provider_prefix("gpt-4o-mini") == "openai/gpt-4o-mini"
    assert _litellm_provider_prefix("command-r-plus") == "cohere_chat/command-r-plus"


def test_normalize_litellm_model_leaves_qualified_and_unknown_models():
    assert normalize_litellm_model("anthropic/claude-opus-4-8") == "anthropic/claude-opus-4-8"
    assert normalize_litellm_model("vendor-model") == "vendor-model"
    assert normalize_litellm_model(None) is None


def test_rewrite_anthropic_model_body_updates_json_model():
    body = json.dumps({"model": "claude-opus-4-8", "messages": []}).encode()

    rewritten = json.loads(_rewrite_model_body(body))

    assert rewritten["model"] == "anthropic/claude-opus-4-8"
    assert rewritten["messages"] == []


def test_rewrite_model_body_updates_openai_model():
    body = json.dumps({"model": "gpt-4o-mini", "messages": []}).encode()

    rewritten = json.loads(_rewrite_model_body(body))

    assert rewritten["model"] == "openai/gpt-4o-mini"


def test_rewrite_anthropic_model_body_ignores_invalid_json():
    body = b"{not-json"

    assert _rewrite_model_body(body) == body


def test_resolve_codex_routing_headers_detects_explicit_chatgpt_account():
    headers, is_chatgpt = _resolve_codex_routing_headers(
        {
            "authorization": "Bearer token",
            "chatgpt-account-id": "acct_123",
        }
    )

    assert is_chatgpt is True
    assert headers["chatgpt-account-id"] == "acct_123"


def test_resolve_codex_routing_headers_detects_chatgpt_account_in_jwt():
    import base64

    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct_from_jwt",
        }
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    headers, is_chatgpt = _resolve_codex_routing_headers(
        {"authorization": f"Bearer header.{encoded}.signature"}
    )

    assert is_chatgpt is True
    assert headers["chatgpt-account-id"] == "acct_from_jwt"


def test_responses_usage_reads_http_response_usage():
    usage = _responses_usage(
        {
            "usage": {
                "input_tokens": 12,
                "output_tokens": 4,
                "input_tokens_details": {"cached_tokens": 3},
                "output_tokens_details": {"reasoning_tokens": 2},
            }
        },
        b"",
    )

    assert usage == {
        "input_tokens": 12,
        "output_tokens": 4,
        "cached_tokens": 3,
        "reasoning_tokens": 2,
    }


def test_responses_usage_reads_sse_completed_event():
    body = b'data: {"type":"response.created"}\n\n' + (
        b'data: {"type":"response.completed","response":{"usage":{"input_tokens":20,'
        b'"output_tokens":5,"input_tokens_details":{"cached_tokens":7}}}}\n\n'
    )

    usage = _responses_usage(None, body)

    assert usage["input_tokens"] == 20
    assert usage["output_tokens"] == 5
    assert usage["cached_tokens"] == 7


def test_responses_usage_reads_codex_event_prefixed_sse():
    body = (
        b"event: response.created\n"
        b'data: {"type":"response.created","response":{"status":"in_progress"}}\n\n'
        b"event: response.completed\n"
        b'data: {"type":"response.completed","response":{"usage":{'
        b'"input_tokens":27,"output_tokens":19,'
        b'"input_tokens_details":{"cached_tokens":3},'
        b'"output_tokens_details":{"reasoning_tokens":12},'
        b'"total_tokens":46}}}\n\n'
    )

    usage = _responses_usage(None, body)

    assert usage == {
        "input_tokens": 27,
        "output_tokens": 19,
        "cached_tokens": 3,
        "reasoning_tokens": 12,
    }


def test_responses_cost_uses_litellm_pricing_for_responses_usage():
    cost = _responses_cost_usd(
        "gpt-4o-mini",
        {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cached_tokens": 100,
            "reasoning_tokens": 50,
        },
    )

    assert cost > 0


def test_normalize_ws_response_create_unwraps_codex_frame():
    frame = {
        "type": "response.create",
        "response": {
            "model": "gpt-5.5",
            "input": "hello",
        },
    }

    body = _normalize_ws_response_create(json.dumps(frame))

    assert body == {
        "model": "gpt-5.5",
        "input": "hello",
        "stream": True,
    }


def test_normalize_ws_response_create_handles_inline_response_body():
    body = _normalize_ws_response_create(
        json.dumps({"type": "response.create", "model": "gpt-5.5", "input": "hello"})
    )

    assert body == {
        "model": "gpt-5.5",
        "input": "hello",
        "stream": True,
    }


def test_ws_response_create_body_rejects_oversized_frame():
    frame = json.dumps({"type": "response.create", "input": "abcdef"})

    with pytest.raises(ProxyPayloadTooLarge):
        _ws_response_create_body(frame, max_bytes=10)


def test_fallback_session_id_is_stable_for_same_conversation_seed():
    headers = {"user-agent": "codex-cli/0.135.0"}
    first = _fallback_session_id(
        "openai-chatgpt",
        "gpt-5.5",
        headers,
        {"input": [{"role": "user", "content": "review this repo"}]},
    )
    second = _fallback_session_id(
        "openai-chatgpt",
        "gpt-5.5",
        headers,
        {
            "input": [
                {"role": "user", "content": "review this repo"},
                {"type": "function_call_output", "output": "growing output"},
            ]
        },
    )

    assert first == second
    assert first.startswith("codex-openai-chatgpt-")


def test_fallback_session_id_splits_different_conversation_seeds():
    headers = {"user-agent": "codex-cli/0.135.0"}

    first = _fallback_session_id(
        "openai-chatgpt", "gpt-5.5", headers, {"input": [{"role": "user", "content": "one"}]}
    )
    second = _fallback_session_id(
        "openai-chatgpt", "gpt-5.5", headers, {"input": [{"role": "user", "content": "two"}]}
    )

    assert first != second


def test_client_label_identifies_wrapped_clients():
    assert _client_label({"user-agent": "codex-cli/0.135.0"}) == "codex"
    assert _client_label({"user-agent": "claude-code/2.1.0"}) == "claude"
    assert _client_label({"user-agent": "unknown"}) == "session"


def test_is_anthropic_native_auth_detects_claude_subscription_headers():
    assert _is_anthropic_native_auth({"user-agent": "claude-code/2.1.0"}) is True
    assert _is_anthropic_native_auth({"authorization": "Bearer sk-ant-oat01-token"}) is True
    assert _is_anthropic_native_auth({"anthropic-version": "2023-06-01"}) is True
    assert _is_anthropic_native_auth({"authorization": "Bearer sk-proj-token"}) is False


def test_anthropic_usage_reads_sse_message_delta_usage():
    body = b'data: {"type":"message_start","message":{"usage":{"input_tokens":10}}}\n\n' + (
        b'data: {"type":"message_delta","usage":{"output_tokens":4,'
        b'"cache_read_input_tokens":3,"cache_creation_input_tokens":2}}\n\n'
    )

    usage = _anthropic_usage(None, body)

    assert usage == {
        "input_tokens": 10,
        "output_tokens": 4,
        "cache_creation_tokens": 2,
        "cache_read_tokens": 3,
    }


def test_anthropic_cost_uses_litellm_pricing():
    cost = _anthropic_cost_usd(
        "claude-sonnet-4-5",
        {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_creation_tokens": 50,
            "cache_read_tokens": 100,
        },
    )

    assert cost > 0


@pytest.mark.asyncio
async def test_read_limited_body_rejects_oversized_request():
    messages = [
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"def", "more_body": False},
    ]

    async def receive():
        return messages.pop(0)

    with pytest.raises(ProxyPayloadTooLarge):
        await _read_limited_body(receive, max_bytes=5)


def test_capped_body_capture_keeps_memory_bounded():
    capture = _CappedBodyCapture(max_bytes=5)

    capture.add(b"abc")
    capture.add(b"def")

    assert capture.body == b"abcde"
    assert capture.truncated is True


def test_httpx_timeout_is_finite():
    timeout = _httpx_timeout()

    assert timeout.connect is not None
    assert timeout.read is not None
    assert timeout.write is not None
    assert timeout.pool is not None


@pytest.mark.asyncio
async def test_model_prefix_middleware_rejects_oversized_json_body():
    app_called = False

    async def app(scope, receive, send):
        nonlocal app_called
        app_called = True

    middleware = ModelPrefixMiddleware(app, max_body_bytes=5)
    sent = []

    async def receive():
        return {
            "type": "http.request",
            "body": b'{"model":"gpt-4o-mini"}',
            "more_body": False,
        }

    async def send(message):
        sent.append(message)

    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
        send,
    )

    assert app_called is False
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["error"]["message"] == "request body too large"

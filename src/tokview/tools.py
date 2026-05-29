"""Parse tool calls out of an LLM request's message history.

An agent loop's tool calls flow through the proxy as structured blocks in the
request/response bodies:
  - the model emits a `tool_use` (Anthropic) / `tool_calls` (OpenAI) to request a tool
  - the client executes it and feeds back a `tool_result` (Anthropic) / `role:tool`
    message (OpenAI)

Both sides end up in the *request* messages array on subsequent turns (the full
conversation is re-sent each turn). So from a single request we can recover, for
each tool call, the tool name + token sizes of its arguments and its result.

We report **token estimates only** — never cost. The provider bills per call, not
per block, and cache discounts make per-block cost meaningless. Token counts are
honest; per-block cost would not be.

`count_tokens(model, text) -> int` is injected so this module stays pure and
testable; the proxy passes a LiteLLM-backed counter.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

CountTokens = Callable[[str, str], int]


def _as_text(value: Any) -> str:
    """Coerce tool arguments / result content into a string for tokenizing.

    Handles: plain strings, dicts/lists (JSON), and Anthropic's list-of-blocks
    result content (e.g. [{"type": "text", "text": "..."}]).
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        # Anthropic tool_result content is often a list of {type,text} blocks.
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or json.dumps(item, default=str)))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(value, dict):
        return json.dumps(value, default=str)
    return str(value)


def parse_completed_tool_calls(
    messages: list[dict[str, Any]] | None,
    model: str,
    count_tokens: CountTokens,
) -> list[dict[str, Any]]:
    """Return one entry per *completed* tool call found in `messages`.

    A call is "completed" when both its invocation (tool_use / tool_calls) and
    its result (tool_result / role:tool) are present. Each entry:
        {"id", "name", "arg_tokens", "result_tokens"}

    Pending calls (invoked but no result yet — e.g. the last turn of a session)
    are intentionally skipped: their result was never sent back, so it never
    cost any input tokens.
    """
    if not messages:
        return []

    uses: dict[str, dict[str, str]] = {}   # id -> {name, arg_text}
    results: dict[str, str] = {}           # id -> result_text

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")

        # --- OpenAI shape ---
        # assistant message with tool_calls
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            tid = tc.get("id")
            fn = tc.get("function") or {}
            if tid:
                uses[tid] = {"name": fn.get("name") or "unknown", "arg_text": _as_text(fn.get("arguments"))}
        # tool result message
        if role == "tool" and msg.get("tool_call_id"):
            results[msg["tool_call_id"]] = _as_text(content)

        # --- Anthropic shape (content is a list of blocks) ---
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "tool_use" and block.get("id"):
                    uses[block["id"]] = {
                        "name": block.get("name") or "unknown",
                        "arg_text": _as_text(block.get("input")),
                    }
                elif btype == "tool_result" and block.get("tool_use_id"):
                    results[block["tool_use_id"]] = _as_text(block.get("content"))

    out: list[dict[str, Any]] = []
    for tid, use in uses.items():
        if tid not in results:
            continue  # pending — skip until the result comes back
        arg_tokens = _safe_count(count_tokens, model, use["arg_text"])
        result_tokens = _safe_count(count_tokens, model, results[tid])
        out.append(
            {
                "id": tid,
                "name": use["name"],
                "arg_tokens": arg_tokens,
                "result_tokens": result_tokens,
            }
        )
    return out


def _safe_count(count_tokens: CountTokens, model: str, text: str) -> int:
    if not text:
        return 0
    try:
        return int(count_tokens(model, text))
    except Exception:
        return max(1, len(text) // 4)  # char-based fallback (~4 chars/token)

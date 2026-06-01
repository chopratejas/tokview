"""Backfill historical usage from local agent logs — no proxy, no API keys.

Claude Code writes a JSONL transcript per session under
``~/.claude/projects/**/*.jsonl``. Every assistant line carries the provider
``usage`` object (input/output/cache tokens) and the message content
(``tool_use`` / ``tool_result`` blocks), so we can reconstruct whole sessions —
including per-tool token estimates — straight from disk, into the *same*
``requests`` + ``tool_calls`` schema the live proxy writes. The TUI and the
browser dashboard then render imported history exactly like live traffic.

Idempotent: requests are keyed by the transcript line uuid and tool calls by
the provider tool id, so re-running an import never double-counts (INSERT OR
IGNORE) and overlap with live-proxied sessions is harmless.

Codex note: ``~/.codex/sessions/*.json`` rollouts do NOT record token counts,
so historical Codex usage can't be reconstructed accurately — only live
``tokview wrap codex`` captures Codex tokens.
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .insights import PricingMap, unit_prices
from .tools import parse_completed_tool_calls

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

CountTokens = Callable[[str, str], int]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _iso_to_ms(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def _fast_count_tokens(model: str, text: str) -> int:
    """Fast token estimate for bulk import. Uses a cached tiktoken encoder
    (good enough for tool-token estimates) and falls back to ~4 chars/token."""
    if not text:
        return 0
    try:

        enc = _encoder()
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)


_ENC = None


def _encoder():
    global _ENC
    if _ENC is None:
        import tiktoken

        _ENC = tiktoken.get_encoding("cl100k_base")
    return _ENC


def estimated_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_creation: int,
    pricing: PricingMap,
) -> float:
    """Estimated equivalent API cost from token counts + the local pricing map.

    Subscription traffic (Claude Code) isn't billed per request, so this is an
    *estimate* of what the same tokens would cost on metered API pricing.
    Non-cache input is priced at the input rate; cache reads/writes at their
    tiers when the model lists them. Unknown models cost 0.0.
    """
    p = unit_prices(model, pricing)
    non_cache_input = max(0, input_tokens)  # usage.input_tokens already excludes cache
    return round(
        non_cache_input * p["input"]
        + output_tokens * p["output"]
        + cache_read * (p["cache_read"] or p["input"])
        + cache_creation * (p["cache_creation"] or p["input"]),
        6,
    )


# --------------------------------------------------------------------------- #
# Claude Code
# --------------------------------------------------------------------------- #
def parse_claude_transcript(
    lines: list[dict[str, Any]],
    pricing: PricingMap,
    count_tokens: CountTokens = _fast_count_tokens,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn one session's transcript lines into (request_rows, tool_rows)."""
    request_rows: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    # session model: the model of the assistant turns (for tool tokenization)
    session_model = "claude-3-5-sonnet"
    last_ts = 0
    session_id = None

    for entry in lines:
        if not isinstance(entry, dict):
            continue
        msg = entry.get("message")
        if isinstance(msg, dict):
            messages.append(msg)
        session_id = entry.get("sessionId") or session_id
        ts_ms = _iso_to_ms(entry.get("timestamp"))
        if ts_ms:
            last_ts = max(last_ts, ts_ms)

        if entry.get("type") != "assistant" or not isinstance(msg, dict):
            continue
        usage = msg.get("usage") or {}
        if not usage:
            continue
        model = msg.get("model") or session_model
        session_model = model
        in_tok = int(usage.get("input_tokens") or 0)
        out_tok = int(usage.get("output_tokens") or 0)
        cache_create = int(usage.get("cache_creation_input_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        request_rows.append(
            {
                "request_id": entry.get("uuid") or msg.get("id"),
                "ts_ms": ts_ms or last_ts or 0,
                "provider": "anthropic",
                "model": model,
                "session_id": session_id,
                "user": None,
                "tags": None,
                "user_agent": "import:claude-code",
                "team_id": None,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cache_creation_tokens": cache_create,
                "cache_read_tokens": cache_read,
                "cache_read_1h_tokens": 0,
                "reasoning_tokens": 0,
                "image_tokens": 0,
                "audio_tokens": 0,
                "output_audio_tokens": None,
                "accepted_prediction_tokens": None,
                "rejected_prediction_tokens": None,
                "cost_usd": estimated_cost(model, in_tok, out_tok, cache_read, cache_create, pricing),
                "cost_estimated": 1,
                "is_stream": 0,
                "completed": 1,
                "latency_ms": None,
                "start_ms": None,
                "ttft_ms": None,
                "status_code": 200,
                "error_message": None,
                "prompt_text": None,
                "response_text": None,
            }
        )

    # tool calls: reuse the live parser over the whole session's message list
    tool_rows: list[dict[str, Any]] = []
    for tc in parse_completed_tool_calls(messages, session_model, count_tokens):
        tool_rows.append(
            {
                "tool_call_id": tc["id"],
                "request_id": None,
                "session_id": session_id,
                "ts_ms": last_ts or 0,
                "provider": "anthropic",
                "model": session_model,
                "tool_name": tc["name"],
                "arg_tokens": tc["arg_tokens"],
                "result_tokens": tc["result_tokens"],
                "total_tokens": tc["arg_tokens"] + tc["result_tokens"],
            }
        )
    return request_rows, tool_rows


def _read_jsonl(path: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        return []
    return out


def import_claude_code(
    con: sqlite3.Connection,
    pricing: PricingMap,
    claude_dir: Path = CLAUDE_PROJECTS,
    count_tokens: CountTokens = _fast_count_tokens,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """Import every Claude Code transcript under ``claude_dir`` into ``con``.

    Returns counts: files, sessions (= files with data), requests, tool_calls.
    """
    files = sorted(glob.glob(os.path.join(str(claude_dir), "**", "*.jsonl"), recursive=True))
    stats = {"files": len(files), "sessions": 0, "requests": 0, "tool_calls": 0}
    req_batch: list[dict[str, Any]] = []
    tool_batch: list[dict[str, Any]] = []

    for i, fp in enumerate(files, 1):
        lines = _read_jsonl(fp)
        if not lines:
            continue
        reqs, tools = parse_claude_transcript(lines, pricing, count_tokens)
        if reqs or tools:
            stats["sessions"] += 1
        req_batch.extend(r for r in reqs if r["request_id"])
        tool_batch.extend(tools)
        if len(req_batch) >= 500 or len(tool_batch) >= 500:
            stats["requests"] += _insert_requests(con, req_batch)
            stats["tool_calls"] += _insert_tools(con, tool_batch)
            req_batch, tool_batch = [], []
        if progress and (i % 200 == 0 or i == len(files)):
            progress(i, len(files))

    stats["requests"] += _insert_requests(con, req_batch)
    stats["tool_calls"] += _insert_tools(con, tool_batch)
    con.commit()
    return stats


# --------------------------------------------------------------------------- #
# DB writers (sync; INSERT OR IGNORE dedupes by request_id / tool_call_id)
# --------------------------------------------------------------------------- #
def _insert_requests(con: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    from .db import REQUEST_COLS

    cols = ", ".join(REQUEST_COLS)
    ph = ", ".join("?" for _ in REQUEST_COLS)
    before = con.total_changes
    con.executemany(
        f"INSERT OR IGNORE INTO requests ({cols}) VALUES ({ph})",
        [[r.get(c) for c in REQUEST_COLS] for r in rows],
    )
    return con.total_changes - before


def _insert_tools(con: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    before = con.total_changes
    con.executemany(
        "INSERT OR IGNORE INTO tool_calls "
        "(tool_call_id, request_id, session_id, ts_ms, provider, model, "
        " tool_name, arg_tokens, result_tokens, total_tokens) "
        "VALUES (:tool_call_id, :request_id, :session_id, :ts_ms, :provider, :model, "
        " :tool_name, :arg_tokens, :result_tokens, :total_tokens)",
        rows,
    )
    return con.total_changes - before

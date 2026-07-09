"""Backfill historical usage from local agent logs — no proxy, no API keys.

Claude Code writes a JSONL transcript per session under
``~/.claude/projects/**/*.jsonl``. Every assistant line carries the provider
``usage`` object (input/output/cache tokens) and the message content
(``tool_use`` / ``tool_result`` blocks), so we can reconstruct whole sessions —
including per-tool token estimates — straight from disk, into the *same*
``requests`` + ``tool_calls`` schema the live proxy writes. The TUI and the
browser dashboard then render imported history exactly like live traffic.

Idempotent: requests are keyed by the message id (falling back to the transcript
line uuid) and tool calls by the provider tool id, so re-running an import never
double-counts (INSERT OR IGNORE) and overlap with live-proxied sessions is
harmless. Keying on the message id also collapses the multiple transcript lines
that recent Claude Code writes for a single assistant response — one line per
content block, each carrying a copy of the same ``usage`` — into one request row,
instead of counting that response's tokens once per block.

Codex (current CLI) writes JSONL rollouts under
``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`` that persist per-turn token
usage as ``token_count`` events (since late 2025). We reconstruct per-turn usage
by diffing the cumulative ``total_token_usage`` between consecutive events —
which also sidesteps the rate-limit-only re-emit over-count — and pair
``function_call`` / ``function_call_output`` items into per-tool estimates.
Older Codex rollouts predate token-usage logging and are skipped.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .insights import PricingMap, unit_prices
from .tools import _as_text, _safe_count, parse_completed_tool_calls

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"

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
                "request_id": msg.get("id") or entry.get("uuid"),
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
# Codex (current CLI rollouts)
# --------------------------------------------------------------------------- #
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)
_SHELL_TOOLS = frozenset(
    {"shell", "exec_command", "local_shell", "local_shell_call", "container.exec"}
)
_SHELL_WRAPPERS = frozenset({"bash", "sh", "zsh"})


def _codex_session_id_from_path(path: str) -> str | None:
    """Recover the conversation id from a ``rollout-...-<uuid>.jsonl`` filename."""
    base = os.path.basename(path)
    m = _UUID_RE.search(base)
    if m:
        return m.group(0)
    return base[: -len(".jsonl")] if base.endswith(".jsonl") else base or None


def _codex_command_parts(arguments: str) -> list[str]:
    """Split a Codex shell call's ``command`` into argv parts.

    Codex passes shell args as a JSON string, e.g.
    ``{"command": ["bash", "-lc", "grep -rn foo src"]}`` (a list) or, less often,
    ``{"command": "grep -rn foo"}`` (a string). For the ``bash -lc "<script>"``
    wrapper the real command lives in the script, so we unwrap and re-split it.
    """
    try:
        parsed = json.loads(arguments) if arguments else None
    except Exception:
        return []
    if not isinstance(parsed, dict):
        return []
    cmd = parsed.get("command") or parsed.get("cmd")
    if isinstance(cmd, list):
        parts = [str(p) for p in cmd]
        if len(parts) >= 3 and parts[0] in _SHELL_WRAPPERS and parts[1] in {"-lc", "-c", "-l"}:
            return _shell_split(parts[2])
        return parts
    if isinstance(cmd, str) and cmd.strip():
        return _shell_split(cmd)
    return []


def _shell_split(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _codex_tool_name(name: str, arguments: str) -> str:
    """Surface the underlying program for Codex shell calls (``grep``, ``pytest``…)."""
    if name not in _SHELL_TOOLS:
        return name or "unknown"
    parts = _codex_command_parts(arguments)
    if not parts:
        return name
    if parts[0] == "rtk" and len(parts) > 1:
        return f"rtk {parts[1]}"
    return os.path.basename(parts[0]) or name


def _codex_meta(payload: dict[str, Any]) -> dict[str, Any]:
    """Session/turn metadata may be nested under ``meta`` depending on version."""
    inner = payload.get("meta")
    return inner if isinstance(inner, dict) else payload


def parse_codex_rollout(
    lines: list[dict[str, Any]],
    pricing: PricingMap,
    count_tokens: CountTokens = _fast_count_tokens,
    default_session_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn one Codex rollout (list of ``RolloutLine`` dicts) into rows.

    Per-turn usage is the delta of the cumulative ``total_token_usage`` between
    consecutive ``token_count`` events. Diffing the running total (rather than
    summing each event's ``last_token_usage``) naturally drops the rate-limit
    re-emits that would otherwise double-count.
    """
    request_rows: list[dict[str, Any]] = []
    tool_uses: dict[str, dict[str, str]] = {}
    tool_outputs: dict[str, str] = {}
    session_id = default_session_id
    model: str | None = None
    last_ts = 0
    prev = {"input": 0, "cached": 0, "output": 0, "reasoning": 0, "total": 0}
    turn_idx = 0

    for entry in lines:
        if not isinstance(entry, dict):
            continue
        rtype = entry.get("type")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        ts_ms = _iso_to_ms(entry.get("timestamp"))
        if ts_ms:
            last_ts = max(last_ts, ts_ms)

        if rtype == "session_meta":
            meta = _codex_meta(payload)
            session_id = meta.get("id") or session_id
            model = model or meta.get("model")
            continue
        if rtype == "turn_context":
            model = _codex_meta(payload).get("model") or model
            continue
        if rtype == "response_item":
            itype = payload.get("type")
            cid = payload.get("call_id") or payload.get("id")
            if itype == "function_call" and cid:
                arg_text = _as_text(payload.get("arguments"))
                tool_uses[cid] = {
                    "name": _codex_tool_name(payload.get("name") or "unknown", arg_text),
                    "arg_text": arg_text,
                }
            elif itype == "function_call_output" and cid:
                tool_outputs[cid] = _as_text(payload.get("output"))
            continue
        if rtype != "event_msg" or payload.get("type") != "token_count":
            continue

        info = payload.get("info")
        total = info.get("total_token_usage") if isinstance(info, dict) else None
        if not isinstance(total, dict):
            continue
        cur = {
            "input": int(total.get("input_tokens") or 0),
            "cached": int(total.get("cached_input_tokens") or 0),
            "output": int(total.get("output_tokens") or 0),
            "reasoning": int(total.get("reasoning_output_tokens") or 0),
            "total": int(total.get("total_tokens") or 0),
        }
        d_in = max(0, cur["input"] - prev["input"])
        d_cached = max(0, cur["cached"] - prev["cached"])
        d_out = max(0, cur["output"] - prev["output"])
        d_reason = max(0, cur["reasoning"] - prev["reasoning"])
        d_total = max(0, cur["total"] - prev["total"])
        prev = cur
        if d_in == 0 and d_out == 0 and d_total == 0:
            continue  # rate-limit-only re-emit — no new usage this event

        mdl = model or "gpt-5"
        non_cache_input = max(0, d_in - d_cached)  # cost only: Codex input includes cached
        request_rows.append(
            {
                "request_id": f"codex:{session_id}:{turn_idx}",
                "ts_ms": ts_ms or last_ts or 0,
                "provider": "openai",
                "model": mdl,
                "session_id": session_id,
                "user": None,
                "tags": None,
                "user_agent": "import:codex",
                "team_id": None,
                # store the full input (incl cached) to match live OpenAI rows
                "input_tokens": d_in,
                "output_tokens": d_out,  # includes reasoning (OpenAI bills it as output)
                "cache_creation_tokens": 0,
                "cache_read_tokens": d_cached,
                "cache_read_1h_tokens": 0,
                "reasoning_tokens": d_reason,
                "image_tokens": 0,
                "audio_tokens": 0,
                "output_audio_tokens": None,
                "accepted_prediction_tokens": None,
                "rejected_prediction_tokens": None,
                "cost_usd": estimated_cost(mdl, non_cache_input, d_out, d_cached, 0, pricing),
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
        turn_idx += 1

    tool_rows: list[dict[str, Any]] = []
    tool_model = model or "gpt-5"
    for cid, use in tool_uses.items():
        if cid not in tool_outputs:
            continue  # pending — result never sent back, so it never cost input
        arg_tokens = _safe_count(count_tokens, tool_model, use["arg_text"])
        result_tokens = _safe_count(count_tokens, tool_model, tool_outputs[cid])
        tool_rows.append(
            {
                "tool_call_id": cid,
                "request_id": None,
                "session_id": session_id,
                "ts_ms": last_ts or 0,
                "provider": "openai",
                "model": tool_model,
                "tool_name": use["name"],
                "arg_tokens": arg_tokens,
                "result_tokens": result_tokens,
                "total_tokens": arg_tokens + result_tokens,
            }
        )
    return request_rows, tool_rows


def import_codex(
    con: sqlite3.Connection,
    pricing: PricingMap,
    codex_dir: Path = CODEX_SESSIONS,
    count_tokens: CountTokens = _fast_count_tokens,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """Import every Codex rollout under ``codex_dir`` into ``con``.

    Returns counts: files, sessions (rollouts with usable usage), requests,
    tool_calls, and skipped_no_usage (older rollouts that predate token logging).
    """
    files = sorted(glob.glob(os.path.join(str(codex_dir), "**", "*.jsonl"), recursive=True))
    stats = {
        "files": len(files),
        "sessions": 0,
        "requests": 0,
        "tool_calls": 0,
        "skipped_no_usage": 0,
    }
    req_batch: list[dict[str, Any]] = []
    tool_batch: list[dict[str, Any]] = []

    for i, fp in enumerate(files, 1):
        lines = _read_jsonl(fp)
        if lines:
            reqs, tools = parse_codex_rollout(
                lines, pricing, count_tokens, default_session_id=_codex_session_id_from_path(fp)
            )
            if reqs:
                stats["sessions"] += 1
                req_batch.extend(r for r in reqs if r["request_id"])
                tool_batch.extend(tools)
            elif tools:
                # rollout predates token-usage logging — skip it whole so we
                # never show tool tokens with no request usage behind them.
                stats["skipped_no_usage"] += 1
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

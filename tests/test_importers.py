"""Tests for historical log import (importers.py).

Synthetic Claude Code transcript + a deterministic word-count tokenizer keep
the assertions exact and off the network.
"""
from __future__ import annotations

import json
import sqlite3

from tokview import importers
from tokview.tui import ensure_schema

PRICING = {
    "claude-3-7-sonnet-20250219": {
        "input_cost_per_token": 3e-6,
        "output_cost_per_token": 15e-6,
        "cache_read_input_token_cost": 0.3e-6,
        "cache_creation_input_token_cost": 3.75e-6,
    },
    "gpt-5-codex": {
        "input_cost_per_token": 1.25e-6,
        "output_cost_per_token": 10e-6,
        "cache_read_input_token_cost": 0.125e-6,
    },
}


def wc(_model, text):  # 1 token per whitespace word
    return len(text.split())


# An assistant turn that calls Read, then a user turn with the tool_result.
TRANSCRIPT = [
    {
        "type": "assistant",
        "uuid": "u1",
        "sessionId": "claude-code-abc",
        "timestamp": "2026-05-30T10:00:00.000Z",
        "message": {
            "id": "msg_1",
            "role": "assistant",
            "model": "claude-3-7-sonnet-20250219",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 40,
                "cache_creation_input_tokens": 2000,
                "cache_read_input_tokens": 500,
            },
            "content": [
                {"type": "text", "text": "reading it"},
                {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": "/x"}},
            ],
        },
    },
    {
        "type": "user",
        "uuid": "u2",
        "sessionId": "claude-code-abc",
        "timestamp": "2026-05-30T10:00:02.000Z",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "alpha beta gamma delta"},
            ],
        },
    },
]


def test_parse_claude_transcript_requests_and_tools():
    reqs, tools = importers.parse_claude_transcript(TRANSCRIPT, PRICING, count_tokens=wc)
    assert len(reqs) == 1
    r = reqs[0]
    # request_id is the message id (msg_1), not the transcript line uuid (u1):
    # recent Claude Code splits one assistant response across several lines with
    # distinct uuids but a shared message id, so keying on the message id is what
    # collapses them to a single request (see test_fanned_content_blocks_*).
    assert r["request_id"] == "msg_1"
    assert r["provider"] == "anthropic"
    assert r["session_id"] == "claude-code-abc"
    assert r["input_tokens"] == 100 and r["output_tokens"] == 40
    assert r["cache_creation_tokens"] == 2000 and r["cache_read_tokens"] == 500
    assert r["cost_estimated"] == 1
    assert r["user_agent"] == "import:claude-code"
    # cost = 100*3e-6 + 40*15e-6 + 500*0.3e-6 + 2000*3.75e-6
    expected = round(100 * 3e-6 + 40 * 15e-6 + 500 * 0.3e-6 + 2000 * 3.75e-6, 6)
    assert r["cost_usd"] == expected

    assert len(tools) == 1
    assert tools[0]["tool_name"] == "Read"
    assert tools[0]["result_tokens"] == 4  # "alpha beta gamma delta"
    assert tools[0]["total_tokens"] == tools[0]["arg_tokens"] + 4


def _write_transcript(dir_path, name, lines):
    fp = dir_path / name
    fp.write_text("\n".join(json.dumps(x) for x in lines))
    return fp


def test_import_claude_code_into_db_and_idempotent(tmp_path):
    proj = tmp_path / "projects"
    proj.mkdir()
    _write_transcript(proj, "s1.jsonl", TRANSCRIPT)

    db_path = tmp_path / "db.sqlite"
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    ensure_schema(con)

    stats = importers.import_claude_code(con, PRICING, claude_dir=proj, count_tokens=wc)
    assert stats["files"] == 1
    assert stats["sessions"] == 1
    assert stats["requests"] == 1
    assert stats["tool_calls"] == 1

    assert con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1
    row = con.execute("SELECT provider, session_id, output_tokens FROM requests").fetchone()
    assert row["provider"] == "anthropic"
    assert row["output_tokens"] == 40

    # re-import: idempotent (INSERT OR IGNORE by message id / tool id)
    stats2 = importers.import_claude_code(con, PRICING, claude_dir=proj, count_tokens=wc)
    assert stats2["requests"] == 0
    assert stats2["tool_calls"] == 0
    assert con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    con.close()


# Recent Claude Code writes one transcript line per content block of a single
# assistant response: same message id, distinct uuids, and the SAME usage copied
# onto every line. Keying requests on the message id collapses them into one row
# so the response's tokens are counted once, not once per block.
FANNED_TRANSCRIPT = [
    {
        "type": "assistant",
        "uuid": "f1",
        "sessionId": "claude-code-fan",
        "timestamp": "2026-05-30T10:00:00.000Z",
        "message": {
            "id": "msg_fan",
            "role": "assistant",
            "model": "claude-3-7-sonnet-20250219",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 40,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 500,
            },
            "content": [{"type": "text", "text": "thinking then acting"}],
        },
    },
    {
        "type": "assistant",
        "uuid": "f2",
        "sessionId": "claude-code-fan",
        "timestamp": "2026-05-30T10:00:00.000Z",
        "message": {
            "id": "msg_fan",
            "role": "assistant",
            "model": "claude-3-7-sonnet-20250219",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 40,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 500,
            },
            "content": [{"type": "tool_use", "id": "toolu_f", "name": "Read", "input": {"path": "/y"}}],
        },
    },
]


def test_fanned_content_blocks_collapse_to_one_request(tmp_path):
    # parse yields one row per assistant line, but both share the message id...
    reqs, _ = importers.parse_claude_transcript(FANNED_TRANSCRIPT, PRICING, count_tokens=wc)
    assert [r["request_id"] for r in reqs] == ["msg_fan", "msg_fan"]

    # ...so INSERT OR IGNORE collapses them: one request, tokens counted once
    # (not 2 rows / 80 output tokens, which is the fan-out double-count).
    proj = tmp_path / "projects"
    proj.mkdir()
    _write_transcript(proj, "fan.jsonl", FANNED_TRANSCRIPT)
    con = sqlite3.connect(str(tmp_path / "db.sqlite"))
    con.row_factory = sqlite3.Row
    ensure_schema(con)
    importers.import_claude_code(con, PRICING, claude_dir=proj, count_tokens=wc)

    assert con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    row = con.execute("SELECT output_tokens, cache_read_tokens FROM requests").fetchone()
    assert row["output_tokens"] == 40
    assert row["cache_read_tokens"] == 500
    con.close()


def test_request_id_falls_back_to_uuid_without_message_id(tmp_path):
    # A line without a message id (older transcripts) still keys on the uuid.
    line = dict(TRANSCRIPT[0])
    line["message"] = {k: v for k, v in TRANSCRIPT[0]["message"].items() if k != "id"}
    reqs, _ = importers.parse_claude_transcript([line], PRICING, count_tokens=wc)
    assert reqs[0]["request_id"] == "u1"


def test_empty_and_malformed_lines_are_skipped(tmp_path):
    proj = tmp_path / "projects"
    proj.mkdir()
    (proj / "bad.jsonl").write_text("not json\n\n{}\n")
    db_path = tmp_path / "db.sqlite"
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    ensure_schema(con)
    stats = importers.import_claude_code(con, PRICING, claude_dir=proj, count_tokens=wc)
    assert stats["requests"] == 0  # no assistant/usage lines
    con.close()


# --------------------------------------------------------------------------- #
# Codex (current CLI rollout format)
# --------------------------------------------------------------------------- #
def _rl(rtype, payload, ts="2025-09-10T10:00:00.000Z"):
    """A Codex RolloutLine envelope."""
    return {"timestamp": ts, "type": rtype, "payload": payload}


def _tok(in_, cached, out, reasoning, total):
    return {
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": in_,
                "cached_input_tokens": cached,
                "output_tokens": out,
                "reasoning_output_tokens": reasoning,
                "total_tokens": total,
            },
            "last_token_usage": {},
            "model_context_window": 272000,
        },
        "rate_limits": None,
    }


SHELL_ARGS = '{"command": ["bash", "-lc", "grep -rn foo src"]}'

CODEX_ROLLOUT = [
    _rl("session_meta", {"id": "codex-sess-1", "cwd": "/repo", "cli_version": "0.41.0"}),
    _rl("turn_context", {"cwd": "/repo", "model": "gpt-5-codex", "effort": "medium"}),
    _rl("response_item", {"type": "message", "role": "user",
                          "content": [{"type": "input_text", "text": "find foo"}]}),
    _rl("response_item", {"type": "function_call", "name": "shell",
                          "arguments": SHELL_ARGS, "call_id": "call_1"}),
    _rl("response_item", {"type": "function_call_output", "call_id": "call_1",
                          "output": "alpha beta gamma"}),
    # turn 1 cumulative usage
    _rl("event_msg", _tok(1000, 200, 300, 100, 1300)),
    # turn 2 cumulative usage (deltas: in 1500, cached 600, out 400, reason 150)
    _rl("event_msg", _tok(2500, 800, 700, 250, 3200)),
    # rate-limit-only re-emit: identical totals -> must be skipped (no double count)
    _rl("event_msg", _tok(2500, 800, 700, 250, 3200)),
]


def test_parse_codex_rollout_requests_and_tools():
    reqs, tools = importers.parse_codex_rollout(CODEX_ROLLOUT, PRICING, count_tokens=wc)

    assert len(reqs) == 2  # re-emit dropped
    r0, r1 = reqs
    assert r0["request_id"] == "codex:codex-sess-1:0"
    assert r1["request_id"] == "codex:codex-sess-1:1"
    assert r0["provider"] == "openai"
    assert r0["model"] == "gpt-5-codex"
    assert r0["user_agent"] == "import:codex"
    assert r0["session_id"] == "codex-sess-1"
    assert r0["cost_estimated"] == 1

    # turn 1: full input incl cached; cache_read is the cached subset
    assert r0["input_tokens"] == 1000 and r0["cache_read_tokens"] == 200
    assert r0["output_tokens"] == 300 and r0["reasoning_tokens"] == 100
    # cost prices the non-cache input (1000-200) + output + cached-at-cache-rate
    exp0 = round((1000 - 200) * 1.25e-6 + 300 * 10e-6 + 200 * 0.125e-6, 6)
    assert r0["cost_usd"] == exp0

    # turn 2: deltas of the cumulative totals
    assert r1["input_tokens"] == 1500 and r1["cache_read_tokens"] == 600
    assert r1["output_tokens"] == 400 and r1["reasoning_tokens"] == 150

    assert len(tools) == 1
    assert tools[0]["tool_name"] == "grep"  # unwrapped from bash -lc "grep ..."
    assert tools[0]["tool_call_id"] == "call_1"
    assert tools[0]["result_tokens"] == 3  # "alpha beta gamma"
    assert tools[0]["arg_tokens"] == len(SHELL_ARGS.split())
    assert tools[0]["total_tokens"] == tools[0]["arg_tokens"] + 3


def test_import_codex_into_db_and_idempotent(tmp_path):
    sessions = tmp_path / "sessions" / "2025" / "09" / "10"
    sessions.mkdir(parents=True)
    fp = sessions / "rollout-2025-09-10T10-00-00-codex-sess-1.jsonl"
    fp.write_text("\n".join(json.dumps(x) for x in CODEX_ROLLOUT))

    db_path = tmp_path / "db.sqlite"
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    ensure_schema(con)

    src = tmp_path / "sessions"
    stats = importers.import_codex(con, PRICING, codex_dir=src, count_tokens=wc)
    assert stats["files"] == 1
    assert stats["sessions"] == 1
    assert stats["requests"] == 2
    assert stats["tool_calls"] == 1
    assert stats["skipped_no_usage"] == 0

    row = con.execute(
        "SELECT provider, session_id, reasoning_tokens FROM requests ORDER BY request_id"
    ).fetchone()
    assert row["provider"] == "openai"
    assert row["session_id"] == "codex-sess-1"
    assert row["reasoning_tokens"] == 100

    # re-import: idempotent (INSERT OR IGNORE by synthesized request_id / call_id)
    stats2 = importers.import_codex(con, PRICING, codex_dir=src, count_tokens=wc)
    assert stats2["requests"] == 0 and stats2["tool_calls"] == 0
    assert con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 2
    con.close()


def test_codex_rollout_without_token_usage_is_skipped(tmp_path):
    # Pre-2025-09 shape: messages + tool calls, but no token_count events.
    old = [
        _rl("session_meta", {"id": "old-sess", "cwd": "/repo"}),
        _rl("response_item", {"type": "function_call", "name": "shell",
                              "arguments": SHELL_ARGS, "call_id": "c1"}),
        _rl("response_item", {"type": "function_call_output", "call_id": "c1",
                              "output": "x y z"}),
    ]
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "rollout-old.jsonl").write_text("\n".join(json.dumps(x) for x in old))

    db_path = tmp_path / "db.sqlite"
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    ensure_schema(con)
    stats = importers.import_codex(con, PRICING, codex_dir=sessions, count_tokens=wc)
    assert stats["requests"] == 0
    assert stats["tool_calls"] == 0  # tools from a no-usage rollout are not imported
    assert stats["skipped_no_usage"] == 1
    assert con.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 0
    con.close()

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
    }
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
    assert r["request_id"] == "u1"
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

    # re-import: idempotent (INSERT OR IGNORE by uuid / tool id)
    stats2 = importers.import_claude_code(con, PRICING, claude_dir=proj, count_tokens=wc)
    assert stats2["requests"] == 0
    assert stats2["tool_calls"] == 0
    assert con.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1
    con.close()


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

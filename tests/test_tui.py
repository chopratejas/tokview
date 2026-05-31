"""Tests for the Textual terminal dashboard (tui.py).

Data-layer tests are synchronous; the app test drives the real Textual app
headlessly via run_test()/pilot.
"""
from __future__ import annotations

import sqlite3
import time

from tokview import tui
from tokview.db import Database
from tokview.tui import TokviewApp

# asyncio_mode = "auto" (pyproject) auto-detects async tests; no per-test mark needed.


def _full_request(**kw):
    base = dict(
        request_id="r1", ts_ms=0, provider="anthropic", model="claude-3-5-sonnet",
        session_id="s1", user=None, tags=None, user_agent="demo", team_id=None,
        input_tokens=0, output_tokens=0, cache_creation_tokens=0, cache_read_tokens=0,
        cache_read_1h_tokens=0, reasoning_tokens=0, image_tokens=0, audio_tokens=0,
        cost_usd=0.0, cost_estimated=0, is_stream=1, completed=1, latency_ms=0,
        start_ms=0, ttft_ms=None, status_code=200, error_message=None,
        prompt_text=None, response_text=None,
    )
    base.update(kw)
    return base


async def _seed(tmp_path):
    db_path = tmp_path / "tui.sqlite"
    db = Database(db_path)
    await db.open()
    now = int(time.time() * 1000)
    await db.insert_request(_full_request(
        request_id="r1", ts_ms=now, session_id="claude-code-7b3a4f",
        input_tokens=1200, output_tokens=400, reasoning_tokens=150, cache_read_tokens=800,
        cost_usd=0.05, latency_ms=1800, start_ms=now - 1800, ttft_ms=250,
    ))
    await db.insert_request(_full_request(
        request_id="r2", ts_ms=now - 5000, session_id="codex-9d2e",
        provider="openai", model="gpt-4o", input_tokens=4000, output_tokens=300,
        cost_usd=0.012, latency_ms=900, start_ms=now - 5900, ttft_ms=120,
    ))
    await db.insert_tool_calls([
        {"tool_call_id": "tc1", "request_id": "r1", "session_id": "claude-code-7b3a4f",
         "ts_ms": now, "provider": "anthropic", "model": "claude-3-5-sonnet",
         "tool_name": "Read", "arg_tokens": 20, "result_tokens": 1240, "total_tokens": 1260},
        {"tool_call_id": "tc2", "request_id": "r1", "session_id": "claude-code-7b3a4f",
         "ts_ms": now, "provider": "anthropic", "model": "claude-3-5-sonnet",
         "tool_name": "Bash", "arg_tokens": 10, "result_tokens": 180, "total_tokens": 190},
    ])
    await db.close()
    return db_path


# ---------- data layer ----------

async def test_data_layer_queries(tmp_path):
    db_path = await _seed(tmp_path)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        sessions = tui.fetch_sessions(con, 10)
        assert {s["session_id"] for s in sessions} == {"claude-code-7b3a4f", "codex-9d2e"}
        # newest session (claude) first
        assert sessions[0]["session_id"] == "claude-code-7b3a4f"

        tools = tui.fetch_session_tools(con, "claude-code-7b3a4f")
        by = {t["tool_name"]: t for t in tools}
        assert by["Read"]["total_tokens"] == 1260
        assert tools[0]["tool_name"] == "Read"  # ordered by total desc

        summary = tui.fetch_session_summary(con, "claude-code-7b3a4f")
        assert summary["requests"] == 1
        assert summary["cache_read"] == 800
        assert summary["reasoning_tokens"] == 150  # output breakdown: reasoning vs answer
        assert summary["output_tokens"] - summary["reasoning_tokens"] == 250  # answer

        ov = tui.fetch_overview(con)
        assert ov["today"]["requests"] >= 1
    finally:
        con.close()


def test_formatters():
    assert tui.fmt_num(1_240_000) == "1.2M"
    assert tui.fmt_num(410) == "410"
    assert tui.fmt_money(0.05) == "$0.05"
    assert tui.fmt_money(0.012, estimated=True).startswith("~$")
    assert tui.bar(50, 100, 10) == "█████·····"
    assert tui.cost_color(2) == tui.BAD
    assert tui.cost_color(0.001) == tui.GOOD


# ---------- the app ----------

async def test_app_populates_and_navigates(tmp_path):
    db_path = await _seed(tmp_path)
    app = TokviewApp(db_path=db_path, limit=10)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable

        sessions = app.query_one("#sessions", DataTable)
        tools = app.query_one("#tools", DataTable)
        assert sessions.row_count == 2
        # newest session selected by default, its tools shown
        assert app.current_sid == "claude-code-7b3a4f"
        assert tools.row_count == 2

        # arrow-key navigation updates the detail selection
        await pilot.press("down")
        await pilot.pause()
        assert app.current_sid == "codex-9d2e"

        # pause toggles
        await pilot.press("p")
        assert app.paused is True


async def test_drill_in_screen_opens_and_populates(tmp_path):
    db_path = await _seed(tmp_path)
    app = TokviewApp(db_path=db_path, limit=10)
    async with app.run_test() as pilot:
        await pilot.pause()
        from tokview.tui import SessionScreen

        # `o` opens the full-screen drill-in for the selected (newest) session
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, SessionScreen)
        assert app.screen.sid == "claude-code-7b3a4f"

        from textual.widgets import DataTable

        # query the active (pushed) screen, not the app's base screen
        dtools = app.screen.query_one("#drill-tools", DataTable)
        dreqs = app.screen.query_one("#drill-requests", DataTable)
        assert dtools.row_count == 2  # Read + Bash
        assert dreqs.row_count == 1

        # esc returns to the master/detail
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, SessionScreen)


async def test_app_initial_session_preselected(tmp_path):
    db_path = await _seed(tmp_path)
    app = TokviewApp(db_path=db_path, limit=10, initial_sid="codex-9d2e")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.current_sid == "codex-9d2e"


async def test_app_empty_db(tmp_path):
    db_path = tmp_path / "empty.sqlite"
    db = Database(db_path)
    await db.open()
    await db.close()
    app = TokviewApp(db_path=db_path, limit=10)
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable

        assert app.query_one("#sessions", DataTable).row_count == 0
        assert app.current_sid is None

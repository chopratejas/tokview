"""Tests for the terminal dashboard renderer."""

from __future__ import annotations

import asyncio
import time

from click.testing import CliRunner

from tokview.cli import (
    _inject_codex_provider_config,
    _render_cli_dashboard,
    _restore_codex_provider_config,
    main,
)
from tokview.db import Database


def _now_ms() -> int:
    return int(time.time() * 1000)


def _request_row(**overrides):
    ts = overrides.pop("ts_ms", _now_ms())
    row = {
        "request_id": f"r-{ts}",
        "ts_ms": ts,
        "provider": "anthropic",
        "model": "anthropic/claude-opus-4-8",
        "session_id": "session-a",
        "user": None,
        "tags": None,
        "user_agent": "test/1.0",
        "team_id": None,
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "cache_read_1h_tokens": 0,
        "reasoning_tokens": 0,
        "image_tokens": 0,
        "audio_tokens": 0,
        "cost_usd": 0.12,
        "cost_estimated": 0,
        "is_stream": 1,
        "completed": 1,
        "latency_ms": 1200,
        "start_ms": ts - 1200,
        "ttft_ms": 300,
        "status_code": 200,
        "error_message": None,
        "prompt_text": None,
        "response_text": None,
    }
    row.update(overrides)
    return row


def _tool_row(**overrides):
    row = {
        "tool_call_id": "tool-1",
        "request_id": "r-1",
        "session_id": "session-a",
        "ts_ms": _now_ms(),
        "provider": "anthropic",
        "model": "anthropic/claude-opus-4-8",
        "tool_name": "Read",
        "arg_tokens": 10,
        "result_tokens": 900,
        "total_tokens": 910,
    }
    row.update(overrides)
    return row


def test_show_overview_is_session_first_with_tools(tmp_path):
    db_path = tmp_path / "tokview.sqlite"

    async def seed() -> None:
        db = Database(db_path)
        await db.open()
        await db.insert_request(_request_row(request_id="r-1"))
        await db.insert_tool_calls([_tool_row(request_id="r-1")])
        await db.close()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(seed())

    out = _render_cli_dashboard(db_path, session_id=None, limit=5)

    assert "SESSION SPEND" in out
    assert "SESSION REQUEST BREAKDOWNS" in out
    assert "TOOL HOTSPOTS" in out
    assert "session-a" in out
    assert "Read" in out


def test_show_session_includes_tool_attribution_and_request_timeline(tmp_path):
    db_path = tmp_path / "tokview.sqlite"

    async def seed() -> None:
        db = Database(db_path)
        await db.open()
        await db.insert_request(_request_row(request_id="r-1"))
        await db.insert_tool_calls([_tool_row(request_id="r-1")])
        await db.close()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(seed())

    out = _render_cli_dashboard(db_path, session_id="session-a", limit=5)

    assert "TOOL TOKEN ATTRIBUTION" in out
    assert "REQUEST TIMELINE" in out
    assert "Read" in out
    assert "Read:910" in out
    assert "anthropic/claude-opus" in out


def test_show_latest_selects_most_recent_session(tmp_path):
    db_path = tmp_path / "tokview.sqlite"
    now = _now_ms()

    async def seed() -> None:
        db = Database(db_path)
        await db.open()
        await db.insert_request(_request_row(request_id="old", session_id="old-session", ts_ms=now))
        await db.insert_request(
            _request_row(request_id="new", session_id="new-session", ts_ms=now + 10)
        )
        await db.close()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(seed())

    out = _render_cli_dashboard(db_path, session_id="latest", limit=5)

    assert "session: new-session" in out


def test_wrap_codex_forwards_all_args_after_codex(monkeypatch):
    captured = {}

    monkeypatch.setattr("tokview.cli._ensure_tokview_started", lambda: None)
    monkeypatch.setattr("tokview.cli._inject_codex_provider_config", lambda _port: None)
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/local/bin/codex" if name == "codex" else None
    )

    def fake_call(argv, env):
        captured["argv"] = argv
        captured["env"] = env
        return 0

    monkeypatch.setattr("subprocess.call", fake_call)

    result = CliRunner().invoke(
        main,
        ["wrap", "codex", "--model", "gpt-5.5", "--search", "literal"],
    )

    assert result.exit_code == 0
    assert captured["argv"] == [
        "/usr/local/bin/codex",
        "--model",
        "gpt-5.5",
        "--search",
        "literal",
    ]
    assert captured["env"]["OPENAI_BASE_URL"] == "http://127.0.0.1:4000/v1"


def test_codex_wrap_config_is_reversible(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config_dir = tmp_path / ".codex"
    config_dir.mkdir()
    config_file = config_dir / "config.toml"
    original = '[profiles.default]\nmodel = "gpt-5.5"\n'
    config_file.write_text(original)

    _inject_codex_provider_config(4000)
    wrapped = config_file.read_text()

    assert 'openai_base_url = "http://127.0.0.1:4000/v1"' in wrapped
    assert "supports_websockets = true" in wrapped
    assert "[model_providers.tokview]" in wrapped
    assert "[profiles.default]" in wrapped

    status, restored_path = _restore_codex_provider_config()

    assert status == "restored"
    assert restored_path == config_file
    assert config_file.read_text() == original


def test_codex_wrap_config_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))

    _inject_codex_provider_config(4000)
    _inject_codex_provider_config(4001)
    config_file = tmp_path / ".codex" / "config.toml"
    wrapped = config_file.read_text()

    assert wrapped.count("tokview proxy") == 2
    assert "http://127.0.0.1:4001/v1" in wrapped
    assert "http://127.0.0.1:4000/v1" not in wrapped

    status, _ = _restore_codex_provider_config()

    assert status == "removed"
    assert not config_file.exists()

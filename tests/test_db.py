"""Tests for the SQLite Database layer."""
from __future__ import annotations

import time

import pytest

from headroom_token_view.db import Database

pytestmark = pytest.mark.asyncio


def _now_ms() -> int:
    return int(time.time() * 1000)


def _row(**overrides):
    ts = overrides.pop("ts_ms", _now_ms())
    base = {
        "request_id": f"r-{ts}",
        "ts_ms": ts,
        "provider": "openai",
        "model": "openai/gpt-4o",
        "session_id": "sess-1",
        "user": None,
        "tags": None,
        "user_agent": "test/1.0",
        "team_id": None,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "cache_read_1h_tokens": 0,
        "reasoning_tokens": 0,
        "image_tokens": 0,
        "audio_tokens": 0,
        "cost_usd": 0.01,
        "cost_estimated": 0,
        "is_stream": 0,
        "completed": 1,
        "latency_ms": 50,
        "status_code": 200,
        "error_message": None,
        "prompt_text": None,
        "response_text": None,
    }
    base.update(overrides)
    return base


async def test_schema_and_pragmas(db: Database):
    """WAL + busy_timeout PRAGMAs should be in effect."""
    async with db.conn.execute("PRAGMA journal_mode") as cur:
        assert (await cur.fetchone())[0].lower() == "wal"
    async with db.conn.execute("PRAGMA busy_timeout") as cur:
        assert int((await cur.fetchone())[0]) >= 1000
    # Tables present
    async with db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ) as cur:
        tables = {r[0] for r in await cur.fetchall()}
    assert {"requests", "daily_rollup", "kv"} <= tables


async def test_insert_and_count(db: Database):
    await db.insert_request(_row())
    assert await db.count_requests() == 1
    await db.insert_request(_row(request_id="r-2"))
    assert await db.count_requests() == 2


async def test_insert_or_ignore_dedups_request_id(db: Database):
    """Duplicate request_id should be a no-op (INSERT OR IGNORE)."""
    await db.insert_request(_row(request_id="same"))
    await db.insert_request(_row(request_id="same"))
    assert await db.count_requests() == 1


async def test_recent_requests_ordering(db: Database):
    now = _now_ms()
    await db.insert_request(_row(request_id="old", ts_ms=now - 60_000))
    await db.insert_request(_row(request_id="new", ts_ms=now))
    rows = await db.recent_requests(limit=10)
    assert [r["request_id"] for r in rows] == ["new", "old"]


async def test_aggregate_excludes_failures(db: Database):
    """4xx/5xx rows shouldn't add to cost totals (spec: failed calls
    don't add to spend; they're still visible in breakdowns)."""
    now = _now_ms()
    await db.insert_request(_row(request_id="ok-1", cost_usd=0.05, status_code=200))
    await db.insert_request(_row(request_id="err-1", cost_usd=0.05, status_code=500))
    agg = await db.aggregate(0, now + 1)
    assert agg["cost_usd"] == pytest.approx(0.05)
    assert agg["requests"] == 1


async def test_by_provider_includes_failures_for_visibility(db: Database):
    now = _now_ms()
    await db.insert_request(_row(request_id="a", provider="anthropic", status_code=500, cost_usd=0))
    await db.insert_request(_row(request_id="b", provider="openai", status_code=200, cost_usd=0.01))
    rows = await db.by_provider(0, now + 1)
    by_p = {r["provider"]: r for r in rows}
    assert "anthropic" in by_p and "openai" in by_p
    assert by_p["anthropic"]["requests"] == 1


async def test_cost_per_minute_buckets(db: Database):
    """Minute buckets group by floor(ts_ms / 60000) * 60000."""
    base = _now_ms() // 60_000 * 60_000  # current minute floor
    await db.insert_request(_row(request_id="m1-a", ts_ms=base + 1_000, cost_usd=0.01))
    await db.insert_request(_row(request_id="m1-b", ts_ms=base + 30_000, cost_usd=0.02))
    await db.insert_request(_row(request_id="m2", ts_ms=base + 60_001, cost_usd=0.05))
    series = await db.cost_per_minute(0, base + 120_000)
    bucket_costs = {r["minute_ms"]: r["cost_usd"] for r in series}
    assert bucket_costs[base] == pytest.approx(0.03)
    assert bucket_costs[base + 60_000] == pytest.approx(0.05)


async def test_health_metrics(db: Database):
    """missing_pricing flags rows with tokens > 0 but cost = 0."""
    await db.insert_request(_row(request_id="ok", cost_usd=0.05, status_code=200))
    # Unrecognized model: tokens recorded, cost stays 0
    await db.insert_request(
        _row(request_id="unknown", model="vendor/secret-v9", cost_usd=0, status_code=200)
    )
    # Estimated row (from disconnect path)
    await db.insert_request(
        _row(request_id="est", cost_usd=0, cost_estimated=1, status_code=500)
    )
    m = await db.health_metrics()
    assert m["total_requests"] == 3
    assert m["missing_pricing"] == 1  # the "unknown" row
    assert m["estimated"] == 1


async def test_recent_errors_filter(db: Database):
    now = _now_ms()
    await db.insert_request(_row(request_id="ok", status_code=200, ts_ms=now))
    await db.insert_request(_row(request_id="err1", status_code=500, ts_ms=now + 1))
    await db.insert_request(_row(request_id="err2", status_code=429, ts_ms=now + 2))
    rows = await db.recent_errors(limit=10)
    ids = [r["request_id"] for r in rows]
    assert "ok" not in ids
    assert set(ids) == {"err1", "err2"}


async def test_by_session_groups_and_concat_models(db: Database):
    now = _now_ms()
    await db.insert_request(_row(request_id="a", session_id="s1", model="claude-3", ts_ms=now))
    await db.insert_request(_row(request_id="b", session_id="s1", model="gpt-4o", ts_ms=now + 1))
    rows = await db.by_session(0, now + 10, limit=10)
    assert len(rows) == 1
    s = rows[0]
    assert s["session_id"] == "s1"
    assert s["requests"] == 2
    assert set(s["models"].split(",")) == {"claude-3", "gpt-4o"}

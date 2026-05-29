"""Tests for the SQLite Database layer."""
from __future__ import annotations

import time

import pytest

from tokview.db import Database

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
        "start_ms": ts - 50,
        "ttft_ms": None,
        "status_code": 200,
        "error_message": None,
        "prompt_text": None,
        "response_text": None,
    }
    base.update(overrides)
    return base


async def test_migration_adds_columns_to_old_db(tmp_path):
    """An old DB without start_ms/ttft_ms should get them via ALTER on open()."""
    import aiosqlite

    db_path = tmp_path / "old.sqlite"
    # Simulate a real v0.0.1 database: the full schema MINUS start_ms/ttft_ms
    # (those are the only columns added in 0.0.2). session_id etc. exist, so
    # the index creation in SCHEMA stays valid.
    conn = await aiosqlite.connect(str(db_path))
    await conn.execute(
        """
        CREATE TABLE requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT UNIQUE NOT NULL,
            ts_ms INTEGER NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            session_id TEXT,
            user TEXT,
            tags TEXT,
            user_agent TEXT,
            team_id TEXT,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_1h_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            image_tokens INTEGER NOT NULL DEFAULT 0,
            audio_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL,
            cost_estimated INTEGER NOT NULL DEFAULT 0,
            is_stream INTEGER NOT NULL,
            completed INTEGER NOT NULL,
            latency_ms INTEGER,
            status_code INTEGER,
            error_message TEXT,
            prompt_text TEXT,
            response_text TEXT
        )
        """
    )
    await conn.execute(
        "INSERT INTO requests (request_id, ts_ms, provider, model, input_tokens, "
        "output_tokens, cost_usd, is_stream, completed) "
        "VALUES ('old-1', 1, 'openai', 'gpt-4o', 10, 5, 0.01, 0, 1)"
    )
    await conn.commit()
    await conn.close()

    # Opening through tokview should migrate it without losing the existing row.
    db = Database(db_path)
    await db.open()
    try:
        async with db.conn.execute("PRAGMA table_info(requests)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        assert "start_ms" in cols
        assert "ttft_ms" in cols
        assert await db.count_requests() == 1  # existing data preserved
    finally:
        await db.close()


async def test_migration_is_idempotent(db: Database):
    """Running _migrate twice must not raise (columns already exist)."""
    await db._migrate()  # fresh DB already has the columns; should be a no-op
    async with db.conn.execute("PRAGMA table_info(requests)") as cur:
        cols = {r[1] for r in await cur.fetchall()}
    assert "start_ms" in cols and "ttft_ms" in cols


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


async def test_session_calls_ordered_oldest_first(db: Database):
    now = _now_ms()
    await db.insert_request(_row(request_id="c2", session_id="s", ts_ms=now + 100))
    await db.insert_request(_row(request_id="c1", session_id="s", ts_ms=now))
    await db.insert_request(_row(request_id="other", session_id="z", ts_ms=now + 50))
    calls = await db.session_calls("s")
    assert [c["request_id"] for c in calls] == ["c1", "c2"]


async def test_latency_percentiles_computes_per_model(db: Database):
    now = _now_ms()
    # 3 calls on one model with known latency + ttft
    for i, (lat, ttft, out_tok) in enumerate([(100, 20, 50), (200, 40, 100), (300, 60, 150)]):
        await db.insert_request(
            _row(
                request_id=f"lat-{i}",
                model="anthropic/claude-3-5-sonnet",
                provider="anthropic",
                ts_ms=now + i,
                latency_ms=lat,
                ttft_ms=ttft,
                output_tokens=out_tok,
                status_code=200,
            )
        )
    out = await db.latency_percentiles(0, now + 1000)
    assert len(out) == 1
    row = out[0]
    assert row["model"] == "anthropic/claude-3-5-sonnet"
    assert row["count"] == 3
    assert row["latency_p50"] == 200.0  # median of 100/200/300
    assert row["ttft_p50"] == 40.0
    assert row["tokens_per_sec_p50"] is not None


async def test_latency_percentiles_excludes_failures(db: Database):
    now = _now_ms()
    await db.insert_request(_row(request_id="ok", latency_ms=100, ttft_ms=10, ts_ms=now))
    await db.insert_request(_row(request_id="err", latency_ms=100, ttft_ms=10, ts_ms=now + 1, status_code=500))
    out = await db.latency_percentiles(0, now + 1000)
    total = sum(r["count"] for r in out)
    assert total == 1  # only the successful call


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

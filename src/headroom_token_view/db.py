"""SQLite layer for Headroom Token View.

Async via aiosqlite. WAL mode, busy_timeout=5000, NORMAL sync. We own
all writes (LiteLLM is in stateless gateway mode), so the budget-decrement
race that drives LiteLLM's "Postgres required" guidance does not apply.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS requests (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id               TEXT UNIQUE NOT NULL,
    ts_ms                    INTEGER NOT NULL,
    provider                 TEXT NOT NULL,
    model                    TEXT NOT NULL,
    session_id               TEXT,
    user                     TEXT,
    tags                     TEXT,
    user_agent               TEXT,
    team_id                  TEXT,
    input_tokens             INTEGER NOT NULL,
    output_tokens            INTEGER NOT NULL,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    cache_read_1h_tokens     INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens         INTEGER NOT NULL DEFAULT 0,
    image_tokens             INTEGER NOT NULL DEFAULT 0,
    audio_tokens             INTEGER NOT NULL DEFAULT 0,
    cost_usd                 REAL NOT NULL,
    cost_estimated           INTEGER NOT NULL DEFAULT 0,
    is_stream                INTEGER NOT NULL,
    completed                INTEGER NOT NULL,
    latency_ms               INTEGER,
    status_code              INTEGER,
    error_message            TEXT,
    prompt_text              TEXT,
    response_text            TEXT
);

CREATE INDEX IF NOT EXISTS idx_req_ts         ON requests(ts_ms);
CREATE INDEX IF NOT EXISTS idx_req_prov_model ON requests(provider, model);
CREATE INDEX IF NOT EXISTS idx_req_session    ON requests(session_id);
CREATE INDEX IF NOT EXISTS idx_req_user       ON requests(user);

CREATE TABLE IF NOT EXISTS daily_rollup (
    day                      TEXT PRIMARY KEY,
    requests_count           INTEGER NOT NULL,
    cost_usd                 REAL NOT NULL,
    input_tokens             INTEGER NOT NULL,
    output_tokens            INTEGER NOT NULL,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    by_provider              TEXT NOT NULL,
    by_model                 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    k          TEXT PRIMARY KEY,
    v          TEXT NOT NULL,
    updated_ms INTEGER NOT NULL
);
"""

# Columns the writer is allowed to set on INSERT (order matters for the SQL builder).
REQUEST_COLS: tuple[str, ...] = (
    "request_id", "ts_ms", "provider", "model", "session_id", "user", "tags",
    "user_agent", "team_id",
    "input_tokens", "output_tokens",
    "cache_creation_tokens", "cache_read_tokens", "cache_read_1h_tokens",
    "reasoning_tokens", "image_tokens", "audio_tokens",
    "cost_usd", "cost_estimated",
    "is_stream", "completed", "latency_ms", "status_code", "error_message",
    "prompt_text", "response_text",
)


class Database:
    """Thin async wrapper around a single aiosqlite connection.

    SQLite is single-writer; one connection serialized through the asyncio
    loop is the standard, race-free pattern.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()  # serialize writes to avoid interleaved transactions

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self.path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        logger.info("htv: opened sqlite at %s", self.path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not opened — call open() first")
        return self._conn

    async def insert_request(self, row: dict[str, Any]) -> None:
        """INSERT OR IGNORE one row into requests. Caller fills only known keys."""
        values = [row.get(col) for col in REQUEST_COLS]
        placeholders = ", ".join("?" for _ in REQUEST_COLS)
        cols_sql = ", ".join(REQUEST_COLS)
        sql = f"INSERT OR IGNORE INTO requests ({cols_sql}) VALUES ({placeholders})"
        async with self._lock:
            await self.conn.execute(sql, values)
            await self.conn.commit()

    async def count_requests(self) -> int:
        async with self.conn.execute("SELECT COUNT(*) FROM requests") as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def recent_requests(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM requests ORDER BY ts_ms DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # ---- aggregates -------------------------------------------------------

    AGG_SQL = """
        SELECT
            COALESCE(SUM(cost_usd), 0.0)              AS cost_usd,
            COUNT(*)                                  AS requests,
            COALESCE(SUM(input_tokens), 0)            AS input_tokens,
            COALESCE(SUM(output_tokens), 0)           AS output_tokens,
            COALESCE(SUM(cache_creation_tokens), 0)   AS cache_creation_tokens,
            COALESCE(SUM(cache_read_tokens), 0)       AS cache_read_tokens,
            COALESCE(SUM(reasoning_tokens), 0)        AS reasoning_tokens
        FROM requests
        WHERE ts_ms BETWEEN ? AND ?
          AND status_code < 400
    """

    async def aggregate(self, since_ms: int, until_ms: int) -> dict[str, Any]:
        async with self.conn.execute(self.AGG_SQL, (since_ms, until_ms)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}

    async def cost_per_minute(self, since_ms: int, until_ms: int) -> list[dict[str, Any]]:
        async with self.conn.execute(
            """
            SELECT
                ((ts_ms / 60000) * 60000) AS minute_ms,
                COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                COUNT(*) AS requests
            FROM requests
            WHERE ts_ms BETWEEN ? AND ?
              AND status_code < 400
            GROUP BY minute_ms
            ORDER BY minute_ms
            """,
            (since_ms, until_ms),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def by_provider(self, since_ms: int, until_ms: int) -> list[dict[str, Any]]:
        async with self.conn.execute(
            """
            SELECT
                provider,
                COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                COUNT(*)                     AS requests,
                COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens
            FROM requests
            WHERE ts_ms BETWEEN ? AND ?
            GROUP BY provider
            ORDER BY cost_usd DESC
            """,
            (since_ms, until_ms),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def by_model(self, since_ms: int, until_ms: int, limit: int = 20) -> list[dict[str, Any]]:
        async with self.conn.execute(
            """
            SELECT
                provider,
                model,
                COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                COUNT(*)                     AS requests
            FROM requests
            WHERE ts_ms BETWEEN ? AND ?
            GROUP BY provider, model
            ORDER BY cost_usd DESC
            LIMIT ?
            """,
            (since_ms, until_ms, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def health_metrics(self) -> dict[str, Any]:
        """Surface guardrail counters for /api/diagnostics.

        - missing_pricing: rows where total_tokens > 0 AND cost = 0.
          That's the canary for an unrecognized model (spec §8).
        - estimated: rows where the cost was tokenizer-estimated, not
          provider-truth (disconnect / failure with token estimate).
        - errors_24h: 4xx/5xx in the last 24h.
        """
        async with self.conn.execute(
            """
            SELECT
                SUM(CASE WHEN (input_tokens + output_tokens) > 0 AND cost_usd = 0 THEN 1 ELSE 0 END) AS missing_pricing,
                SUM(cost_estimated)                                                                 AS estimated,
                SUM(CASE WHEN status_code >= 400 AND ts_ms >= ? THEN 1 ELSE 0 END)                 AS errors_24h,
                COUNT(*)                                                                            AS total_requests
            FROM requests
            """,
            (int((__import__('time').time() - 86400) * 1000),),
        ) as cur:
            row = await cur.fetchone()
            return {
                "missing_pricing": int(row["missing_pricing"] or 0),
                "estimated": int(row["estimated"] or 0),
                "errors_24h": int(row["errors_24h"] or 0),
                "total_requests": int(row["total_requests"] or 0),
            }

    async def recent_errors(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self.conn.execute(
            """
            SELECT request_id, ts_ms, provider, model, status_code, error_message
            FROM requests
            WHERE status_code >= 400
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def by_session(self, since_ms: int, until_ms: int, limit: int = 20) -> list[dict[str, Any]]:
        async with self.conn.execute(
            """
            SELECT
                session_id,
                COALESCE(SUM(cost_usd), 0.0)  AS cost_usd,
                COUNT(*)                      AS requests,
                MIN(ts_ms)                    AS first_ts_ms,
                MAX(ts_ms)                    AS last_ts_ms,
                GROUP_CONCAT(DISTINCT model)  AS models
            FROM requests
            WHERE ts_ms BETWEEN ? AND ?
              AND session_id IS NOT NULL
            GROUP BY session_id
            ORDER BY cost_usd DESC
            LIMIT ?
            """,
            (since_ms, until_ms, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

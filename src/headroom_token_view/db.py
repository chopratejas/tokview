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

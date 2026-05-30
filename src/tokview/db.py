"""SQLite layer for tokview.

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


def _percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile. Returns None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return round(ordered[lo] * (1 - frac) + ordered[hi] * frac, 2)


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
    start_ms                 INTEGER,
    ttft_ms                  INTEGER,
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

CREATE TABLE IF NOT EXISTS tool_calls (
    tool_call_id   TEXT PRIMARY KEY,          -- provider tool id; dedupes re-sends across turns
    request_id     TEXT,                      -- the LLM call where this pair was first observed
    session_id     TEXT,
    ts_ms          INTEGER NOT NULL,
    provider       TEXT,
    model          TEXT,
    tool_name      TEXT NOT NULL,
    arg_tokens     INTEGER NOT NULL DEFAULT 0,
    result_tokens  INTEGER NOT NULL DEFAULT 0,
    total_tokens   INTEGER NOT NULL DEFAULT 0  -- token estimates only; never cost (see tools.py)
);

CREATE INDEX IF NOT EXISTS idx_tool_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_name    ON tool_calls(tool_name);
"""

# Columns the writer is allowed to set on INSERT (order matters for the SQL builder).
REQUEST_COLS: tuple[str, ...] = (
    "request_id",
    "ts_ms",
    "provider",
    "model",
    "session_id",
    "user",
    "tags",
    "user_agent",
    "team_id",
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "cache_read_1h_tokens",
    "reasoning_tokens",
    "image_tokens",
    "audio_tokens",
    "cost_usd",
    "cost_estimated",
    "is_stream",
    "completed",
    "latency_ms",
    "start_ms",
    "ttft_ms",
    "status_code",
    "error_message",
    "prompt_text",
    "response_text",
)

# Columns added after the initial release. Each entry is (name, SQL type).
# Applied via ALTER TABLE on open() so existing on-disk databases pick them
# up without a manual migration. New columns MUST be nullable (no NOT NULL
# without a default) — SQLite can't add a NOT NULL column to a populated table.
_MIGRATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("start_ms", "INTEGER"),
    ("ttft_ms", "INTEGER"),
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
        await self._migrate()
        await self._conn.commit()
        logger.info("tokview: opened sqlite at %s", self.path)

    async def _migrate(self) -> None:
        """Add columns introduced after the initial schema, for old DBs.

        Fresh databases already have these (they're in SCHEMA). Existing
        on-disk databases get them via ALTER TABLE. Idempotent: skips any
        column that already exists.
        """
        async with self.conn.execute("PRAGMA table_info(requests)") as cur:
            existing = {row[1] for row in await cur.fetchall()}  # row[1] = column name
        for name, sql_type in _MIGRATION_COLUMNS:
            if name not in existing:
                await self.conn.execute(f"ALTER TABLE requests ADD COLUMN {name} {sql_type}")
                logger.info("tokview: migrated — added requests.%s", name)

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
        # cost_estimated=1 rows have a known-unknowable cost (e.g. mid-stream
        # disconnect); exclude them from the missing_pricing canary so we
        # only flag rows that should have priced but didn't.
        async with self.conn.execute(
            """
            SELECT
                SUM(CASE WHEN (input_tokens + output_tokens) > 0
                          AND cost_usd = 0
                          AND cost_estimated = 0
                         THEN 1 ELSE 0 END)                                                          AS missing_pricing,
                SUM(cost_estimated)                                                                  AS estimated,
                SUM(CASE WHEN status_code >= 400 AND ts_ms >= ? THEN 1 ELSE 0 END)                  AS errors_24h,
                COUNT(*)                                                                             AS total_requests
            FROM requests
            """,
            (int((__import__("time").time() - 86400) * 1000),),
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

    async def by_session(
        self, since_ms: int, until_ms: int, limit: int = 20
    ) -> list[dict[str, Any]]:
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

    async def session_calls(self, session_id: str, limit: int = 500) -> list[dict[str, Any]]:
        """All calls in a session, oldest first — the rows behind a waterfall."""
        async with self.conn.execute(
            "SELECT * FROM requests WHERE session_id = ? ORDER BY ts_ms ASC LIMIT ?",
            (session_id, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ---- tool calls -------------------------------------------------------

    async def insert_tool_calls(self, rows: list[dict[str, Any]]) -> None:
        """INSERT OR IGNORE tool-call rows. Keyed on tool_call_id so the same
        call re-sent on later turns is recorded exactly once."""
        if not rows:
            return
        sql = (
            "INSERT OR IGNORE INTO tool_calls "
            "(tool_call_id, request_id, session_id, ts_ms, provider, model, "
            " tool_name, arg_tokens, result_tokens, total_tokens) "
            "VALUES (:tool_call_id, :request_id, :session_id, :ts_ms, :provider, :model, "
            " :tool_name, :arg_tokens, :result_tokens, :total_tokens)"
        )
        async with self._lock:
            await self.conn.executemany(sql, rows)
            await self.conn.commit()

    async def session_tool_breakdown(self, session_id: str) -> list[dict[str, Any]]:
        """Per-tool token totals for one session (token estimates only)."""
        async with self.conn.execute(
            """
            SELECT
                tool_name,
                COUNT(*)                          AS calls,
                COALESCE(SUM(arg_tokens), 0)      AS arg_tokens,
                COALESCE(SUM(result_tokens), 0)   AS result_tokens,
                COALESCE(SUM(total_tokens), 0)    AS total_tokens
            FROM tool_calls
            WHERE session_id = ?
            GROUP BY tool_name
            ORDER BY total_tokens DESC
            """,
            (session_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def global_tool_breakdown(self, limit: int = 12) -> list[dict[str, Any]]:
        """Top tools across all sessions by total token estimate."""
        async with self.conn.execute(
            """
            SELECT
                tool_name,
                COUNT(*)                          AS calls,
                COALESCE(SUM(arg_tokens), 0)      AS arg_tokens,
                COALESCE(SUM(result_tokens), 0)   AS result_tokens,
                COALESCE(SUM(total_tokens), 0)    AS total_tokens
            FROM tool_calls
            GROUP BY tool_name
            ORDER BY total_tokens DESC
            LIMIT ?
            """,
            (limit,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def total_tool_tokens(self) -> int:
        async with self.conn.execute("SELECT COALESCE(SUM(total_tokens), 0) FROM tool_calls") as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def count_tool_calls(self) -> int:
        async with self.conn.execute("SELECT COUNT(*) FROM tool_calls") as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def latency_percentiles(self, since_ms: int, until_ms: int) -> list[dict[str, Any]]:
        """Per-model latency + TTFT percentiles (p50/p95) over a window.

        SQLite has no PERCENTILE function, so we pull the per-model arrays and
        compute in Python. Volumes here are laptop-scale (thousands of rows),
        so this is fine. Only successful, completed calls are considered.
        """
        async with self.conn.execute(
            """
            SELECT provider, model, latency_ms, ttft_ms, output_tokens, ttft_ms IS NOT NULL AS has_ttft
            FROM requests
            WHERE ts_ms BETWEEN ? AND ?
              AND status_code < 400
              AND latency_ms IS NOT NULL
            """,
            (since_ms, until_ms),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        by_model: dict[tuple[str, str], dict[str, list[float]]] = {}
        for r in rows:
            key = (r["provider"] or "unknown", r["model"] or "unknown")
            acc = by_model.setdefault(key, {"latency": [], "ttft": [], "tps": []})
            if r["latency_ms"] is not None:
                acc["latency"].append(float(r["latency_ms"]))
            if r["ttft_ms"] is not None:
                acc["ttft"].append(float(r["ttft_ms"]))
            # tokens/sec over the post-first-token window when we have TTFT
            if r["ttft_ms"] is not None and r["latency_ms"] and r["output_tokens"]:
                gen_ms = max(1.0, float(r["latency_ms"]) - float(r["ttft_ms"]))
                acc["tps"].append(float(r["output_tokens"]) / (gen_ms / 1000.0))

        out: list[dict[str, Any]] = []
        for (provider, model), acc in by_model.items():
            out.append(
                {
                    "provider": provider,
                    "model": model,
                    "count": len(acc["latency"]),
                    "latency_p50": _percentile(acc["latency"], 50),
                    "latency_p95": _percentile(acc["latency"], 95),
                    "ttft_p50": _percentile(acc["ttft"], 50),
                    "ttft_p95": _percentile(acc["ttft"], 95),
                    "tokens_per_sec_p50": _percentile(acc["tps"], 50),
                }
            )
        out.sort(key=lambda d: d["count"], reverse=True)
        return out

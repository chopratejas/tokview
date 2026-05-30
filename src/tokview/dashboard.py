"""FastAPI app for the tokview dashboard backend.

Iter 3 exposes:
  /api/health, /api/status      — liveness probes
  /api/calls                    — paginated recent rows
  /api/summary                  — totals (today/week/MTD) + cache tokens + minute series
  /api/providers, /api/models   — cost breakdowns
  /api/sessions                 — per-session aggregates
  /                             — embedded vanilla-JS dashboard (Iter 5 swaps in SvelteKit)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .db import Database
from .insights import compute_insights, load_pricing_map, model_whatif
from .pubsub import PubSub

logger = logging.getLogger(__name__)


def _find_web_build() -> Path | None:
    """Locate the bundled SvelteKit build directory.

    Looks in (a) installed package resources (`_web/`), (b) the editable-dev
    sibling `../../../web/build/` from this file's parent. Returns None if
    neither exists; the caller then falls back to the inline HTML. Editable
    installs can contain hatch_build.py's placeholder index; ignore that so the
    functional inline dashboard remains available.
    """

    def usable(path: Path) -> bool:
        index = path / "index.html"
        if not index.exists():
            return False
        try:
            text = index.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return True
        return "The SvelteKit dashboard wasn't included in this install" not in text

    try:
        pkg_web = files("tokview") / "_web"
        # files() yields a Traversable; coerce to a real path
        path = Path(str(pkg_web))
        if usable(path):
            return path
    except (FileNotFoundError, ModuleNotFoundError):
        pass
    dev_path = Path(__file__).resolve().parent.parent.parent / "web" / "build"
    if usable(dev_path):
        return dev_path
    return None


def _utc_today_start_ms() -> int:
    now = datetime.now(UTC)
    start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    return int(start.timestamp() * 1000)


def _utc_month_start_ms() -> int:
    now = datetime.now(UTC)
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    return int(start.timestamp() * 1000)


def _now_ms() -> int:
    return int(time.time() * 1000)


def build_app(db: Database, pubsub: PubSub | None = None) -> FastAPI:
    started_at = time.time()
    app = FastAPI(
        title="tokview",
        version=__version__,
        docs_url=None,
        redoc_url=None,
    )
    app.state.db = db
    app.state.pubsub = pubsub
    # Pricing map is frozen for the process lifetime (we disable LiteLLM's
    # runtime cost-map fetch), so load it once. Tests can override via
    # app.state.pricing.
    app.state.pricing = load_pricing_map()

    @app.get("/api/health")
    def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "uptime_seconds": round(time.time() - started_at, 1),
            }
        )

    @app.get("/api/status")
    async def status() -> JSONResponse:
        count = await db.count_requests()
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "uptime_seconds": round(time.time() - started_at, 1),
                "requests_logged": count,
            }
        )

    @app.get("/api/calls")
    async def calls(limit: int = Query(default=50, ge=1, le=500)) -> JSONResponse:
        rows: list[dict[str, Any]] = await db.recent_requests(limit=limit)
        return JSONResponse({"calls": rows, "count": len(rows)})

    @app.get("/api/summary")
    async def summary() -> JSONResponse:
        now = _now_ms()
        today_start = _utc_today_start_ms()
        week_start = now - 7 * 86_400_000
        month_start = _utc_month_start_ms()
        hour_start = now - 60 * 60_000

        today, week, mtd = await _gather_aggs(
            db,
            [
                (today_start, now),
                (week_start, now),
                (month_start, now),
            ],
        )
        minute_series = await db.cost_per_minute(hour_start, now)

        return JSONResponse(
            {
                "today": today,
                "week": week,
                "mtd": mtd,
                "minute_series": minute_series,
                "as_of_ms": now,
            }
        )

    @app.get("/api/providers")
    async def providers(
        since: int = Query(default=None, description="unix ms (default: month-to-date)"),
    ) -> JSONResponse:
        now = _now_ms()
        since_ms = since if since is not None else _utc_month_start_ms()
        rows = await db.by_provider(since_ms, now)
        return JSONResponse({"providers": rows, "since_ms": since_ms, "until_ms": now})

    @app.get("/api/models")
    async def models(
        since: int = Query(default=None),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> JSONResponse:
        now = _now_ms()
        since_ms = since if since is not None else _utc_month_start_ms()
        rows = await db.by_model(since_ms, now, limit=limit)
        return JSONResponse({"models": rows, "since_ms": since_ms, "until_ms": now})

    @app.get("/api/sessions")
    async def sessions(
        since: int = Query(default=None),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> JSONResponse:
        now = _now_ms()
        since_ms = since if since is not None else _utc_month_start_ms()
        rows = await db.by_session(since_ms, now, limit=limit)
        return JSONResponse({"sessions": rows, "since_ms": since_ms, "until_ms": now})

    @app.get("/api/sessions/{session_id}")
    async def session_detail(session_id: str) -> JSONResponse:
        """One session: ordered calls + summary + model what-if + per-tool tokens."""
        calls = await db.session_calls(session_id)
        if not calls:
            return JSONResponse(
                {
                    "session_id": session_id,
                    "calls": [],
                    "summary": None,
                    "insights": [],
                    "tools": [],
                }
            )

        starts = [c["start_ms"] or c["ts_ms"] for c in calls]
        ends = [c["ts_ms"] for c in calls]
        summary = {
            "session_id": session_id,
            "requests": len(calls),
            "cost_usd": round(sum(float(c["cost_usd"] or 0) for c in calls), 6),
            "input_tokens": sum(int(c["input_tokens"] or 0) for c in calls),
            "output_tokens": sum(int(c["output_tokens"] or 0) for c in calls),
            "errors": sum(1 for c in calls if (c["status_code"] or 200) >= 400),
            "first_ts_ms": min(starts),
            "last_ts_ms": max(ends),
            "span_ms": max(ends) - min(starts),
            "models": sorted({c["model"] for c in calls if c["model"]}),
        }
        whatif = model_whatif(calls, app.state.pricing)
        tools = await db.session_tool_breakdown(session_id)
        return JSONResponse(
            {
                "session_id": session_id,
                "calls": calls,
                "summary": summary,
                "insights": whatif,
                "tools": tools,
            }
        )

    @app.get("/api/insights")
    async def insights() -> JSONResponse:
        """Deterministic savings coach over recent activity (no model calls)."""
        rows = await db.recent_requests(limit=5000)
        items = compute_insights(rows, app.state.pricing)
        total = round(sum(i.get("estimated_savings_usd", 0.0) for i in items), 4)
        return JSONResponse({"insights": items, "total_estimated_savings_usd": total})

    @app.get("/api/tools")
    async def tools(limit: int = Query(default=12, ge=1, le=100)) -> JSONResponse:
        """Global tool hotspots — top tools by token estimate, plus a hotspot
        callout when one tool dominates (the re-sent-result cost)."""
        rows = await db.global_tool_breakdown(limit)
        total = await db.total_tool_tokens()
        hotspot = None
        if rows and total > 0:
            top = rows[0]
            share = round(100 * int(top["total_tokens"] or 0) / total, 1)
            if share >= 40:
                hotspot = {
                    "tool_name": top["tool_name"],
                    "total_tokens": int(top["total_tokens"] or 0),
                    "share_pct": share,
                }
        return JSONResponse(
            {"tools": rows, "total_tool_tokens": total, "hotspot": hotspot}
        )

    @app.get("/api/latency")
    async def latency(
        since: int = Query(default=None),
    ) -> JSONResponse:
        now = _now_ms()
        since_ms = since if since is not None else _utc_month_start_ms()
        rows = await db.latency_percentiles(since_ms, now)
        return JSONResponse({"models": rows, "since_ms": since_ms, "until_ms": now})

    @app.get("/api/diagnostics")
    async def diagnostics() -> JSONResponse:
        """Guardrails dashboard endpoint. Surfaces:
        - missing_pricing: rows that completed with tokens > 0 but cost = 0
          (canary for an unrecognized model — spec §8).
        - estimated: rows with tokenizer-estimated cost (disconnect path).
        - errors_24h: failed calls in the last 24h.
        - subscribers: live SSE subscriber count.
        """
        metrics = await db.health_metrics()
        errors = await db.recent_errors(limit=10)
        return JSONResponse(
            {
                "metrics": metrics,
                "recent_errors": errors,
                "subscribers": pubsub.subscriber_count if pubsub is not None else 0,
                "uptime_seconds": round(time.time() - started_at, 1),
                "version": __version__,
            }
        )

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        """Server-Sent Events: stream every spend event as it lands.

        SPA subscribes on connect; on reconnect after a network blip it
        replays missed activity via /api/calls?since=<last_seen_ts_ms>
        (the SSE channel itself does not retain history — see spec §5.4).
        """
        if pubsub is None:
            return StreamingResponse(iter(()), media_type="text/event-stream")

        async def stream() -> AsyncIterator[bytes]:
            async with pubsub.subscribe() as q:
                # initial hello so the client knows it's connected
                yield b": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=15.0)
                    except TimeoutError:
                        # heartbeat to keep intermediaries from closing the conn
                        yield b": ping\n\n"
                        continue
                    event = msg.get("event", "message") if isinstance(msg, dict) else "message"
                    payload = msg.get("row") if isinstance(msg, dict) else msg
                    yield f"event: {event}\n".encode()
                    yield b"data: " + json.dumps(payload, default=str).encode() + b"\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # Serve the embedded SvelteKit SPA at /. If it isn't built yet (dev
    # mode without `npm run build`), fall back to the inline HTML below.
    web_build = _find_web_build()
    if web_build is not None:
        logger.info("tokview: serving SPA from %s", web_build)
        app.mount("/", StaticFiles(directory=web_build, html=True), name="spa")
    else:
        logger.warning(
            "tokview: SvelteKit build not found; falling back to inline HTML. "
            "Run `npm run build` in web/ to enable the full dashboard."
        )

        @app.get("/", response_class=HTMLResponse)
        def index() -> HTMLResponse:
            return HTMLResponse(_INDEX_HTML)

    return app


async def _gather_aggs(db: Database, windows: list[tuple[int, int]]) -> list[dict[str, Any]]:
    """Run several aggregate windows; SQLite is single-conn so we go sequentially."""
    results: list[dict[str, Any]] = []
    for since_ms, until_ms in windows:
        results.append(await db.aggregate(since_ms, until_ms))
    return results


# Embedded vanilla-JS dashboard. Iter 5 swaps in the SvelteKit build; until
# then this gives a real "tokview" experience without a build step.
_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>tokview</title>
  <style>
    :root {
      --bg: #0e1117;
      --panel: #161b22;
      --panel-2: #1c2330;
      --border: #2a313c;
      --text: #e6edf3;
      --text-dim: #8b949e;
      --accent: #7c5cff;
      --accent-2: #58a6ff;
      --good: #3fb950;
      --warn: #d29922;
      --bad:  #f85149;
      --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; color: var(--text); background: var(--bg); }
    header { display: flex; align-items: center; justify-content: space-between; padding: 14px 24px; border-bottom: 1px solid var(--border); }
    header h1 { margin: 0; font-size: 16px; font-weight: 600; letter-spacing: 0.2px; }
    header h1 .brand-glyph { color: var(--accent); margin-right: 6px; }
    .live { font-size: 11px; color: var(--text-dim); display: flex; align-items: center; gap: 6px; }
    .live .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--good); box-shadow: 0 0 8px var(--good); animation: pulse 2s infinite; }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
    main { padding: 24px; max-width: 1200px; margin: 0 auto; }
    .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin-bottom: 24px; }
    .tile { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
    .tile .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--text-dim); margin-bottom: 8px; }
    .tile .value { font-size: 22px; font-weight: 600; font-family: var(--mono); }
    .tile .sub { font-size: 11px; color: var(--text-dim); margin-top: 4px; }
    .chart-wrap { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin-bottom: 24px; }
    .chart-wrap h3 { margin: 0 0 10px 0; font-size: 12px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--text-dim); font-weight: 500; }
    svg.minutes { width: 100%; height: 80px; display: block; }
    .grid-3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; margin-bottom: 24px; }
    .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
    .panel h3 { margin: 0 0 10px 0; font-size: 12px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--text-dim); font-weight: 500; }
    .row { display: flex; justify-content: space-between; align-items: center; padding: 4px 0; font-family: var(--mono); font-size: 13px; }
    .row .name { color: var(--text); flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .row .meta { color: var(--text-dim); margin: 0 10px; font-size: 11px; }
    .row .cost { color: var(--accent-2); }
    .row-btn { width: 100%; border: 0; background: transparent; color: inherit; padding: 0; text-align: left; cursor: pointer; border-radius: 6px; }
    .row-btn:hover { background: var(--panel-2); }
    .bar-wrap { background: var(--panel-2); border-radius: 4px; height: 4px; margin-top: 4px; overflow: hidden; }
    .bar { background: var(--accent); height: 100%; }
    table { width: 100%; font-family: var(--mono); font-size: 12px; border-collapse: collapse; }
    th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
    th { color: var(--text-dim); font-weight: 500; text-transform: uppercase; letter-spacing: 0.6px; font-size: 10px; }
    td.num { text-align: right; }
    td.muted { color: var(--text-dim); }
    td.link { cursor: pointer; }
    td.link:hover { color: var(--accent-2); text-decoration: underline; }
    .empty { color: var(--text-dim); padding: 8px 0; font-style: italic; }
    .session-detail { margin-bottom: 24px; }
    .session-stats { display: flex; flex-wrap: wrap; gap: 18px; margin-bottom: 12px; font-family: var(--mono); }
    .session-stats span { color: var(--text-dim); }
    .session-tools { margin-top: 12px; }
    code { font-family: var(--mono); font-size: 12px; background: var(--panel-2); padding: 2px 6px; border-radius: 4px; }
    .footer { color: var(--text-dim); font-size: 11px; margin-top: 32px; padding-top: 16px; border-top: 1px solid var(--border); }
    .footer a { color: var(--accent-2); text-decoration: none; }
  </style>
</head>
<body>
  <header>
    <h1><span class="brand-glyph">◆</span>tokview</h1>
    <span class="live"><span class="dot"></span> live · polling every 3s</span>
  </header>
  <main>
    <div class="tiles">
      <div class="tile"><div class="label">Today</div><div class="value" id="today-cost">$0.00</div><div class="sub" id="today-sub">0 calls · 0 tokens</div></div>
      <div class="tile"><div class="label">This Week</div><div class="value" id="week-cost">$0.00</div><div class="sub" id="week-sub">0 calls</div></div>
      <div class="tile"><div class="label">Month to date</div><div class="value" id="mtd-cost">$0.00</div><div class="sub" id="mtd-sub">0 calls</div></div>
      <div class="tile"><div class="label">Cache reads (mtd)</div><div class="value" id="cache-tokens">0</div><div class="sub">tokens served from cache</div></div>
    </div>

    <div class="chart-wrap">
      <h3>Cost / minute · last hour</h3>
      <svg class="minutes" id="minutes" viewBox="0 0 600 80" preserveAspectRatio="none"></svg>
    </div>

    <div class="grid-3">
      <div class="panel"><h3>By provider</h3><div id="providers"></div></div>
      <div class="panel"><h3>By model</h3><div id="models"></div></div>
      <div class="panel"><h3>By session</h3><div id="sessions"></div></div>
    </div>

    <div class="panel session-detail" id="session-detail" style="display:none"></div>

    <div class="panel">
      <h3>Live tail · last 20</h3>
      <table>
        <thead><tr><th>ts</th><th>provider</th><th>model</th><th class="num">in&rarr;out</th><th class="num">cost</th><th>session</th></tr></thead>
        <tbody id="calls"></tbody>
      </table>
    </div>

    <div class="footer">
      Point your apps at the proxy:
      <code>ANTHROPIC_BASE_URL=http://localhost:4000</code> &middot;
      <code>OPENAI_BASE_URL=http://localhost:4000/v1</code> &middot;
      <code>GOOGLE_BASE_URL=http://localhost:4000</code>
      &middot; <a href="/api/health">/api/health</a>
    </div>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const fmtUsd = (n) => '$' + (n || 0).toFixed(2);
    const fmtUsdSmall = (n) => '$' + (n || 0).toFixed(4);
    const fmtNum = (n) => (n || 0).toLocaleString();
    const fmtTs = (ms) => new Date(ms).toLocaleTimeString(undefined, { hour12: false });

    async function refresh() {
      try {
        const [summary, providers, models, sessions, calls] = await Promise.all([
          fetch('/api/summary').then(r => r.json()),
          fetch('/api/providers').then(r => r.json()),
          fetch('/api/models').then(r => r.json()),
          fetch('/api/sessions').then(r => r.json()),
          fetch('/api/calls?limit=20').then(r => r.json()),
        ]);
        renderSummary(summary);
        renderBreakdown('providers', providers.providers, (r) => r.provider, summary.mtd.cost_usd || 1);
        renderBreakdown('models', models.models, (r) => r.model, summary.mtd.cost_usd || 1);
        renderSessions(sessions.sessions, summary.mtd.cost_usd || 1);
        renderCalls(calls.calls);
      } catch (e) {
        console.error('refresh failed', e);
      }
    }

    function renderSummary(s) {
      $('today-cost').textContent = fmtUsd(s.today.cost_usd);
      $('today-sub').textContent = `${fmtNum(s.today.requests)} calls · ${fmtNum((s.today.input_tokens || 0) + (s.today.output_tokens || 0))} tokens`;
      $('week-cost').textContent = fmtUsd(s.week.cost_usd);
      $('week-sub').textContent = `${fmtNum(s.week.requests)} calls`;
      $('mtd-cost').textContent = fmtUsd(s.mtd.cost_usd);
      $('mtd-sub').textContent = `${fmtNum(s.mtd.requests)} calls`;
      $('cache-tokens').textContent = fmtNum(s.mtd.cache_read_tokens || 0);
      renderMinutes(s.minute_series, s.as_of_ms);
    }

    function renderMinutes(series, asOfMs) {
      const svg = $('minutes');
      svg.innerHTML = '';
      const bucketMs = 60_000;
      const buckets = 60;
      const startMs = (Math.floor(asOfMs / bucketMs) - buckets + 1) * bucketMs;
      const data = new Array(buckets).fill(0);
      for (const row of series) {
        const idx = Math.floor((row.minute_ms - startMs) / bucketMs);
        if (idx >= 0 && idx < buckets) data[idx] = row.cost_usd;
      }
      const max = Math.max(...data, 0.001);
      const w = 600, h = 80, barW = w / buckets;
      data.forEach((v, i) => {
        const barH = (v / max) * (h - 4);
        const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        rect.setAttribute('x', i * barW + 1);
        rect.setAttribute('y', h - barH);
        rect.setAttribute('width', Math.max(1, barW - 2));
        rect.setAttribute('height', barH);
        rect.setAttribute('fill', v > 0 ? '#7c5cff' : '#2a313c');
        svg.appendChild(rect);
      });
    }

    function renderBreakdown(containerId, rows, label, total) {
      const el = $(containerId);
      if (!rows || rows.length === 0) { el.innerHTML = '<div class="empty">no calls yet</div>'; return; }
      el.innerHTML = rows.map(r => {
        const pct = total > 0 ? Math.min(100, (r.cost_usd / total) * 100) : 0;
        return `
          <div>
            <div class="row">
              <span class="name">${escape(label(r))}</span>
              <span class="meta">${fmtNum(r.requests)}</span>
              <span class="cost">${fmtUsdSmall(r.cost_usd)}</span>
            </div>
            <div class="bar-wrap"><div class="bar" style="width:${pct.toFixed(1)}%"></div></div>
          </div>
        `;
      }).join('');
    }

    function renderSessions(rows, total) {
      const el = $('sessions');
      if (!rows || rows.length === 0) { el.innerHTML = '<div class="empty">no sessions yet</div>'; return; }
      el.innerHTML = rows.map(r => {
        const pct = total > 0 ? Math.min(100, (r.cost_usd / total) * 100) : 0;
        const sid = (r.session_id || '').slice(0, 12) + '…';
        return `
          <button class="row-btn" data-session="${escape(r.session_id || '')}">
            <div class="row">
              <span class="name" title="${escape(r.session_id || '')}">${escape(sid)}</span>
              <span class="meta">${fmtNum(r.requests)}</span>
              <span class="cost">${fmtUsdSmall(r.cost_usd)}</span>
            </div>
            <div class="bar-wrap"><div class="bar" style="width:${pct.toFixed(1)}%"></div></div>
          </button>
        `;
      }).join('');
      el.querySelectorAll('[data-session]').forEach(btn => {
        btn.addEventListener('click', () => openSession(btn.dataset.session));
      });
    }

    function renderCalls(rows) {
      const el = $('calls');
      if (!rows || rows.length === 0) { el.innerHTML = '<tr><td colspan="6" class="empty">no calls yet</td></tr>'; return; }
      el.innerHTML = rows.map(r => `
        <tr>
          <td class="muted">${fmtTs(r.ts_ms)}</td>
          <td>${escape(r.provider || '')}</td>
          <td>${escape(r.model || '')}</td>
          <td class="num">${fmtNum(r.input_tokens)} &rarr; ${fmtNum(r.output_tokens)}</td>
          <td class="num">${fmtUsdSmall(r.cost_usd)}</td>
          <td class="muted ${r.session_id ? 'link' : ''}" data-session="${escape(r.session_id || '')}">${escape((r.session_id || '').slice(0, 16))}</td>
        </tr>
      `).join('');
      el.querySelectorAll('[data-session]').forEach(cell => {
        if (cell.dataset.session) cell.addEventListener('click', () => openSession(cell.dataset.session));
      });
    }

    async function openSession(sessionId) {
      const el = $('session-detail');
      el.style.display = 'block';
      el.innerHTML = '<h3>Session trace</h3><div class="empty">loading…</div>';
      try {
        const data = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`).then(r => r.json());
        if (!data.summary) {
          el.innerHTML = '<h3>Session trace</h3><div class="empty">no calls in this session</div>';
          return;
        }
        const s = data.summary;
        const tools = data.tools || [];
        const calls = data.calls || [];
        el.innerHTML = `
          <h3>Session trace · <span class="muted">${escape(data.session_id)}</span></h3>
          <div class="session-stats">
            <div><span>calls</span> ${fmtNum(s.requests)}</div>
            <div><span>cost</span> ${fmtUsdSmall(s.cost_usd)}</div>
            <div><span>tokens</span> ${fmtNum((s.input_tokens || 0) + (s.output_tokens || 0))}</div>
            <div><span>errors</span> ${fmtNum(s.errors)}</div>
          </div>
          ${tools.length ? `
            <div class="session-tools">
              <h3>Tools used · token estimates</h3>
              <table>
                <thead><tr><th>tool</th><th class="num">calls</th><th class="num">args</th><th class="num">results</th><th class="num">total</th></tr></thead>
                <tbody>${tools.map(t => `
                  <tr><td>${escape(t.tool_name)}</td><td class="num">${fmtNum(t.calls)}</td><td class="num muted">${fmtNum(t.arg_tokens)}</td><td class="num">${fmtNum(t.result_tokens)}</td><td class="num">${fmtNum(t.total_tokens)}</td></tr>
                `).join('')}</tbody>
              </table>
            </div>
          ` : '<div class="empty">no completed tool calls recorded for this session yet</div>'}
          <div class="session-tools">
            <h3>Calls</h3>
            <table>
              <thead><tr><th>ts</th><th>model</th><th class="num">in&rarr;out</th><th class="num">cost</th><th class="num">status</th></tr></thead>
              <tbody>${calls.map(c => `
                <tr><td class="muted">${fmtTs(c.ts_ms)}</td><td>${escape(c.model || '')}</td><td class="num">${fmtNum(c.input_tokens)} &rarr; ${fmtNum(c.output_tokens)}</td><td class="num">${fmtUsdSmall(c.cost_usd)}</td><td class="num">${c.status_code || 200}</td></tr>
              `).join('')}</tbody>
            </table>
          </div>
        `;
      } catch (e) {
        el.innerHTML = '<h3>Session trace</h3><div class="empty">failed to load session</div>';
      }
    }

    function escape(s) {
      return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }

    // Initial paint + a slow safety-net poll. Real updates come from SSE.
    refresh();
    setInterval(refresh, 15000);

    // Subscribe to live spend events. EventSource auto-reconnects on drop;
    // /api/calls?since= is the documented backfill path (spec §5.4).
    let lastEventMs = Date.now();
    function subscribe() {
      const es = new EventSource('/api/events');
      es.addEventListener('spend', (e) => {
        lastEventMs = Date.now();
        try { applySpend(JSON.parse(e.data)); }
        catch (err) { console.warn('bad spend event', err); refresh(); }
      });
      es.onerror = () => {
        // EventSource handles reconnect itself; we just refresh on resume
        setTimeout(refresh, 1000);
      };
    }

    // Optimistic UI update on a new spend row.
    let lastSummary = null;
    function applySpend(row) {
      if (!row) return;
      // Bump tiles (only successful + within-window calls add to cost; failures still pop into the live tail)
      if (lastSummary) {
        if (row.completed && row.status_code < 400) {
          ['today', 'week', 'mtd'].forEach(k => {
            lastSummary[k].cost_usd = (lastSummary[k].cost_usd || 0) + (row.cost_usd || 0);
            lastSummary[k].requests = (lastSummary[k].requests || 0) + 1;
            lastSummary[k].input_tokens = (lastSummary[k].input_tokens || 0) + (row.input_tokens || 0);
            lastSummary[k].output_tokens = (lastSummary[k].output_tokens || 0) + (row.output_tokens || 0);
            lastSummary[k].cache_read_tokens = (lastSummary[k].cache_read_tokens || 0) + (row.cache_read_tokens || 0);
          });
          renderSummary(lastSummary);
        }
      }
      // Prepend to the live tail
      const tbody = $('calls');
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td class="muted">${fmtTs(row.ts_ms)}</td>
        <td>${escape(row.provider || '')}</td>
        <td>${escape(row.model || '')}</td>
        <td class="num">${fmtNum(row.input_tokens)} &rarr; ${fmtNum(row.output_tokens)}</td>
        <td class="num">${fmtUsdSmall(row.cost_usd)}</td>
        <td class="muted">${escape((row.session_id || '').slice(0, 16))}</td>
      `;
      tr.style.background = 'rgba(124, 92, 255, 0.08)';
      setTimeout(() => { tr.style.background = ''; }, 1500);
      if (tbody.firstChild) tbody.insertBefore(tr, tbody.firstChild); else tbody.appendChild(tr);
      while (tbody.children.length > 20) tbody.removeChild(tbody.lastChild);
    }

    // Wrap renderSummary so we keep a reference for optimistic updates.
    const _renderSummary = renderSummary;
    renderSummary = function (s) { lastSummary = s; _renderSummary(s); };

    subscribe();
  </script>
</body>
</html>"""

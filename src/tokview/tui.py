"""Interactive terminal dashboard for tokview, built on Textual.

Master/detail layout: a clickable sessions list on the left; live per-session
detail (summary + per-tool token breakdown + request tail) on the right; a
color-coded spend bar across the top. Textual's compositor diffs the screen,
so updates don't flicker, and the data refreshes on an interval while keeping
your current selection.

Run via `tokview show --watch`.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Static

# Brand palette (matches the web dashboard).
ACCENT = "#7c5cff"
ACCENT2 = "#58a6ff"
GOOD = "#3fb950"
WARN = "#d29922"
BAD = "#f85149"
CYAN = "#39c5cf"
DIM = "#8b949e"

REFRESH_SECONDS = 1.5


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #
def fmt_money(value: float | None, estimated: bool = False) -> str:
    v = float(value or 0)
    s = f"${v:,.2f}" if v >= 0.005 else f"${v:.4f}"
    return ("~" + s) if estimated else s


def fmt_num(n: int | float | None) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def fmt_age(ts_ms: int | None) -> str:
    if not ts_ms:
        return "-"
    secs = max(0, int(time.time() - ts_ms / 1000))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86_400:
        return f"{secs // 3600}h"
    return f"{secs // 86_400}d"


def fmt_time(ts_ms: int | None) -> str:
    return datetime.fromtimestamp((ts_ms or 0) / 1000).strftime("%H:%M:%S")


def clip(s: str | None, n: int) -> str:
    s = s or "-"
    return s if len(s) <= n else s[: n - 1] + "…"


def bar(value: float, total: float, width: int = 12) -> str:
    if total <= 0:
        return ""
    filled = max(0, min(width, round(width * value / total)))
    return "█" * filled + "·" * (width - filled)


def cost_color(value: float | None) -> str:
    v = float(value or 0)
    if v >= 1:
        return BAD
    if v >= 0.1:
        return WARN
    return GOOD


# --------------------------------------------------------------------------- #
# data layer (synchronous sqlite; queries are sub-ms at laptop scale)
# --------------------------------------------------------------------------- #
def _utc_today_start_ms() -> int:
    now = datetime.now(UTC)
    return int(datetime(now.year, now.month, now.day, tzinfo=UTC).timestamp() * 1000)


def _utc_month_start_ms() -> int:
    now = datetime.now(UTC)
    return int(datetime(now.year, now.month, 1, tzinfo=UTC).timestamp() * 1000)


def _agg(con: sqlite3.Connection, since_ms: int, until_ms: int) -> dict:
    row = con.execute(
        """
        SELECT COALESCE(SUM(cost_usd), 0) cost_usd, COUNT(*) requests,
               COALESCE(SUM(input_tokens + output_tokens), 0) tokens,
               COALESCE(SUM(cache_read_tokens), 0) cache_read,
               COALESCE(SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END), 0) errors
        FROM requests WHERE ts_ms BETWEEN ? AND ?
        """,
        (since_ms, until_ms),
    ).fetchone()
    return dict(row) if row else {}


def fetch_overview(con: sqlite3.Connection) -> dict:
    now = int(time.time() * 1000)
    return {
        "today": _agg(con, _utc_today_start_ms(), now),
        "week": _agg(con, now - 7 * 86_400_000, now),
        "month": _agg(con, _utc_month_start_ms(), now),
        "rate": con.execute(
            "SELECT COUNT(*) c FROM requests WHERE ts_ms >= ?", (now - 60_000,)
        ).fetchone()["c"],
    }


def fetch_sessions(con: sqlite3.Connection, limit: int) -> list[dict]:
    rows = con.execute(
        """
        SELECT r.session_id,
               COUNT(*) requests,
               COALESCE(SUM(r.cost_usd), 0) cost_usd,
               COALESCE(SUM(r.input_tokens + r.output_tokens), 0) tokens,
               COALESCE(SUM(CASE WHEN r.status_code >= 400 THEN 1 ELSE 0 END), 0) errors,
               MAX(r.ts_ms) last_ts_ms,
               COALESCE(t.tool_tokens, 0) tool_tokens
        FROM requests r
        LEFT JOIN (
            SELECT session_id, SUM(total_tokens) tool_tokens
            FROM tool_calls GROUP BY session_id
        ) t ON t.session_id = r.session_id
        WHERE r.session_id IS NOT NULL
        GROUP BY r.session_id
        ORDER BY last_ts_ms DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_session_summary(con: sqlite3.Connection, sid: str) -> dict | None:
    row = con.execute(
        """
        SELECT COUNT(*) requests,
               COALESCE(SUM(cost_usd), 0) cost_usd,
               COALESCE(SUM(input_tokens), 0) input_tokens,
               COALESCE(SUM(output_tokens), 0) output_tokens,
               COALESCE(SUM(cache_read_tokens), 0) cache_read,
               COALESCE(SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END), 0) errors,
               MIN(ts_ms) first_ts_ms, MAX(ts_ms) last_ts_ms,
               GROUP_CONCAT(DISTINCT model) models
        FROM requests WHERE session_id = ?
        """,
        (sid,),
    ).fetchone()
    if not row or row["requests"] == 0:
        return None
    return dict(row)


def fetch_session_tools(con: sqlite3.Connection, sid: str, limit: int = 12) -> list[dict]:
    rows = con.execute(
        """
        SELECT tool_name, COUNT(*) calls,
               COALESCE(SUM(arg_tokens), 0) arg_tokens,
               COALESCE(SUM(result_tokens), 0) result_tokens,
               COALESCE(SUM(total_tokens), 0) total_tokens
        FROM tool_calls WHERE session_id = ?
        GROUP BY tool_name ORDER BY total_tokens DESC LIMIT ?
        """,
        (sid, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_session_requests(con: sqlite3.Connection, sid: str, limit: int = 30) -> list[dict]:
    rows = con.execute(
        """
        SELECT ts_ms, model, input_tokens, output_tokens, cache_read_tokens,
               cost_usd, cost_estimated, status_code, ttft_ms, latency_ms
        FROM requests WHERE session_id = ?
        ORDER BY ts_ms DESC LIMIT ?
        """,
        (sid, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# the app
# --------------------------------------------------------------------------- #
class TokviewApp(App):
    """tokview live terminal dashboard."""

    CSS = """
    Screen { background: #0e1117; }

    #statbar {
        height: 3;
        padding: 1 2 0 2;
        background: #161b22;
        border-bottom: solid #7c5cff;
    }

    #body { height: 1fr; }

    #sessions {
        width: 40%;
        background: #0e1117;
        border-right: solid #2a313c;
    }
    #sessions > .datatable--header { color: #8b949e; text-style: bold; }
    #sessions > .datatable--cursor { background: #7c5cff; color: #ffffff; text-style: bold; }

    #detail { width: 1fr; padding: 0 1; }
    #summary { height: auto; padding: 1 1 0 1; }
    .label {
        color: #8b949e;
        text-style: bold;
        padding: 1 0 0 1;
    }
    DataTable { height: auto; background: #0e1117; }
    #tools > .datatable--header,
    #requests > .datatable--header { color: #8b949e; text-style: bold; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit"),
        Binding("p", "toggle_pause", "Pause"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("g", "jump_top", "Top"),
    ]

    def __init__(self, db_path: Path, limit: int = 25, initial_sid: str | None = None) -> None:
        super().__init__()
        self.db_path = db_path
        self.limit = limit
        # Preferred selection on first populate; falls back to the newest session.
        self.current_sid: str | None = initial_sid
        self.paused = False
        self._session_keys: list[str] = []

    # ---- layout ----
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="statbar")
        with Horizontal(id="body"):
            yield DataTable(id="sessions", cursor_type="row", zebra_stripes=False)
            with VerticalScroll(id="detail"):
                yield Static(id="summary")
                yield Static("TOOLS  ·  token estimates", classes="label")
                yield DataTable(id="tools", cursor_type="none")
                yield Static("REQUESTS  ·  newest first", classes="label")
                yield DataTable(id="requests", cursor_type="none")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "tokview"
        self.sub_title = "live"
        sessions = self.query_one("#sessions", DataTable)
        sessions.add_columns("session", "cost", "tok", "tools", "err", "last")
        tools = self.query_one("#tools", DataTable)
        tools.add_columns("tool", "calls", "args", "results", "total", "share")
        reqs = self.query_one("#requests", DataTable)
        reqs.add_columns("time", "model", "in→out", "ttft", "cache", "cost")
        self.refresh_data()
        self.set_interval(REFRESH_SECONDS, self._tick)

    # ---- refresh ----
    def _tick(self) -> None:
        if not self.paused:
            self.refresh_data()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path))
        con.row_factory = sqlite3.Row
        return con

    def refresh_data(self) -> None:
        try:
            con = self._connect()
        except sqlite3.Error:
            return
        try:
            self._render_statbar(fetch_overview(con))
            self._render_sessions(fetch_sessions(con, self.limit))
            if self.current_sid:
                self._render_detail(con, self.current_sid)
        finally:
            con.close()

    def _render_statbar(self, ov: dict) -> None:
        t, w, m = ov["today"], ov["week"], ov["month"]
        line = Text()
        line.append("TODAY ", style=DIM)
        line.append(fmt_money(t.get("cost_usd")), style=f"bold {cost_color(t.get('cost_usd'))}")
        line.append(f"  {t.get('requests', 0)} calls", style=DIM)
        line.append("     WEEK ", style=DIM)
        line.append(fmt_money(w.get("cost_usd")), style=ACCENT2)
        line.append("     MTD ", style=DIM)
        line.append(fmt_money(m.get("cost_usd")), style=ACCENT2)
        line.append("     CACHE ", style=DIM)
        line.append(f"{fmt_num(m.get('cache_read'))} reads", style=CYAN)
        line.append("     RATE ", style=DIM)
        line.append(f"{ov.get('rate', 0)}/min", style=GOOD if ov.get("rate") else DIM)
        if self.paused:
            line.append("   ⏸ PAUSED", style=f"bold {WARN}")
        self.query_one("#statbar", Static).update(line)

    def _render_sessions(self, rows: list[dict]) -> None:
        table = self.query_one("#sessions", DataTable)
        prev = self.current_sid
        table.clear()
        self._session_keys = []
        for r in rows:
            sid = r["session_id"]
            self._session_keys.append(sid)
            cost = float(r["cost_usd"] or 0)
            err = int(r["errors"] or 0)
            table.add_row(
                Text(clip(sid, 22), style=ACCENT),
                Text(fmt_money(cost), style=cost_color(cost)),
                fmt_num(r["tokens"]),
                Text(fmt_num(r["tool_tokens"]), style=ACCENT),
                Text(str(err), style=BAD if err else DIM),
                Text(fmt_age(r["last_ts_ms"]), style=DIM),
                key=sid,
            )
        if not rows:
            self.current_sid = None
            self._render_empty_detail()
            return
        # Preserve selection across refresh; default to the newest session.
        target = prev if prev in self._session_keys else self._session_keys[0]
        self.current_sid = target
        try:
            table.move_cursor(row=self._session_keys.index(target), animate=False)
        except Exception:
            pass

    def _render_empty_detail(self) -> None:
        self.query_one("#summary", Static).update(
            Text("no sessions yet — run `tokview wrap claude` or `tokview wrap codex`", style=DIM)
        )
        self.query_one("#tools", DataTable).clear()
        self.query_one("#requests", DataTable).clear()

    def _render_detail(self, con: sqlite3.Connection, sid: str) -> None:
        summary = fetch_session_summary(con, sid)
        summ = self.query_one("#summary", Static)
        if not summary:
            self._render_empty_detail()
            return
        dur = max(0, (summary["last_ts_ms"] - summary["first_ts_ms"]) // 1000)
        dur_s = f"{dur // 60}m{dur % 60:02d}s" if dur >= 60 else f"{dur}s"
        head = Text()
        head.append(clip(sid, 48), style=f"bold {ACCENT}")
        head.append("\n")
        head.append(f"{summary['requests']} calls", style="bold")
        head.append(f"  ·  {fmt_num(summary['input_tokens'] + summary['output_tokens'])} tok", style=DIM)
        head.append(f"  ·  {fmt_money(summary['cost_usd'])}", style=cost_color(summary["cost_usd"]))
        head.append(f"  ·  {dur_s}", style=DIM)
        head.append(f"  ·  {fmt_num(summary['cache_read'])} cache", style=CYAN)
        if summary["errors"]:
            head.append(f"  ·  ⚠ {summary['errors']} errors", style=f"bold {BAD}")
        head.append(f"\n{clip(summary['models'], 70)}", style=DIM)
        summ.update(head)

        # tools
        tools = fetch_session_tools(con, sid)
        tbl = self.query_one("#tools", DataTable)
        tbl.clear()
        max_total = max((int(t["total_tokens"] or 0) for t in tools), default=0)
        for t in tools:
            total = int(t["total_tokens"] or 0)
            share = bar(total, max_total, 12)
            tbl.add_row(
                Text(clip(t["tool_name"], 26), style=ACCENT2),
                str(t["calls"]),
                fmt_num(t["arg_tokens"]),
                fmt_num(t["result_tokens"]),
                Text(fmt_num(total), style="bold"),
                Text(share, style=ACCENT),
            )
        if not tools:
            tbl.add_row(Text("no tool calls recorded yet", style=DIM), "", "", "", "", "")

        # requests
        reqs = fetch_session_requests(con, sid)
        rtbl = self.query_one("#requests", DataTable)
        rtbl.clear()
        for c in reqs:
            err = (c["status_code"] or 200) >= 400
            rtbl.add_row(
                Text(fmt_time(c["ts_ms"]), style=DIM),
                Text(clip(c["model"], 22), style=BAD if err else "white"),
                f"{fmt_num(c['input_tokens'])}→{fmt_num(c['output_tokens'])}",
                Text(f"{c['ttft_ms']}ms" if c["ttft_ms"] else "-", style=DIM),
                Text(fmt_num(c["cache_read_tokens"]), style=CYAN if c["cache_read_tokens"] else DIM),
                Text(fmt_money(c["cost_usd"], c["cost_estimated"]), style=cost_color(c["cost_usd"])),
            )
        if not reqs:
            rtbl.add_row(Text("no requests", style=DIM), "", "", "", "", "")

    # ---- events ----
    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "sessions":
            return
        sid = event.row_key.value
        if sid and sid != self.current_sid:
            self.current_sid = sid
            con = self._connect()
            try:
                self._render_detail(con, sid)
            finally:
                con.close()

    # ---- actions ----
    def action_toggle_pause(self) -> None:
        self.paused = not self.paused
        self.refresh_data()

    def action_refresh_now(self) -> None:
        self.refresh_data()

    def action_jump_top(self) -> None:
        table = self.query_one("#sessions", DataTable)
        if table.row_count:
            table.move_cursor(row=0, animate=False)


def run(db_path: Path, limit: int = 25, initial_sid: str | None = None) -> None:
    """Launch the interactive dashboard."""
    TokviewApp(db_path=db_path, limit=limit, initial_sid=initial_sid).run()

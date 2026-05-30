"""`tokview` command-line interface.

Commands:
    tokview start [--foreground] [--allow-remote]
    tokview stop
    tokview status
    tokview show [--session SESSION_ID] [--watch]
    tokview logs [--tail]
    tokview export --since YYYY-MM-DD [--format csv|json]
    tokview reset [--yes]
    tokview version
    tokview config-path
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import click

from . import __version__
from .config import DEFAULT_CONFIG_PATH, DEFAULT_DIR, TokviewConfig
from .config import load as load_config
from .server import serve

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})

PID_FILE = DEFAULT_DIR / "tokview.pid"
LOG_FILE = DEFAULT_DIR / "tokview.log"


@click.group(help="tokview — drop-in LLM proxy and cost dashboard.")
@click.version_option(version=__version__, prog_name="tokview")
def main() -> None:
    """Command group."""


@main.command()
@click.option(
    "--foreground", "-f",
    is_flag=True,
    default=False,
    help="Run in the foreground instead of daemonizing (good for debugging).",
)
@click.option(
    "--allow-remote",
    is_flag=True,
    default=False,
    help="Allow non-loopback bind addresses. v1 has NO authentication — use only if you accept the risk.",
)
def start(foreground: bool, allow_remote: bool) -> None:
    """Start the proxy and optional browser dashboard."""
    tokview = load_config()

    # Trust boundary per spec §6.4: non-loopback bind requires both the config value
    # and the --allow-remote flag.
    non_loopback = tokview.proxy.bind not in LOOPBACK or tokview.dashboard.bind not in LOOPBACK
    if non_loopback and not allow_remote:
        raise click.ClickException(
            f"Config requests a non-loopback bind (proxy.bind={tokview.proxy.bind!r}, "
            f"dashboard.bind={tokview.dashboard.bind!r}) but --allow-remote was not passed.\n"
            "v1 has no authentication; team deploys belong on the Postgres+Docker path.\n"
            "If you accept the risk, run: tokview start --allow-remote"
        )

    # If already running, refuse. We skip this check when we ARE the child
    # the parent just spawned (TOKVIEW_INTERNAL_SPAWN=1).
    if os.environ.get("TOKVIEW_INTERNAL_SPAWN") != "1":
        pid = _read_pid()
        if pid and _pid_running(pid):
            raise click.ClickException(
                f"tokview is already running (pid={pid}). Use 'tokview stop' first, or 'tokview status'."
            )

    DEFAULT_DIR.mkdir(parents=True, exist_ok=True)

    if not foreground:
        # Self-spawn in foreground, detached. Avoids os.fork weirdness on macOS
        # and keeps the asyncio loop entirely in the child process.
        env = dict(os.environ)
        env["TOKVIEW_INTERNAL_SPAWN"] = "1"
        log_fh = LOG_FILE.open("ab")
        child = subprocess.Popen(
            [sys.argv[0], "start", "--foreground"] + (["--allow-remote"] if allow_remote else []),
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
        PID_FILE.write_text(str(child.pid))
        proxy_url = f"http://{tokview.proxy.bind}:{tokview.proxy.port}"
        browser_url = f"http://{tokview.dashboard.bind}:{tokview.dashboard.port}"
        click.echo(_banner_running(tokview, child.pid))
        click.echo(
            f"\nNext: tokview show --watch\n"
            f"Logs: {LOG_FILE}\n"
            f"Proxy: {proxy_url}\n"
            f"Browser dashboard (optional): {browser_url}"
        )
        return

    # Foreground path. If we're the spawned child, our PID is already in the
    # PID file (parent wrote it). Make sure it points to *us* in case the
    # parent's child-PID guess was wrong.
    if os.environ.get("TOKVIEW_INTERNAL_SPAWN") == "1":
        PID_FILE.write_text(str(os.getpid()))

    _print_banner(tokview)
    try:
        asyncio.run(serve(tokview))
    except KeyboardInterrupt:
        click.echo("\nstopped.")
    finally:
        # Best-effort PID cleanup. stop/reset also handle this; this covers
        # the case where the child exited on its own (Ctrl-C, supervisor).
        if PID_FILE.exists():
            try:
                if PID_FILE.read_text().strip() == str(os.getpid()):
                    PID_FILE.unlink()
            except OSError:
                pass


@main.command()
def stop() -> None:
    """Stop a running tokview process."""
    pid = _read_pid()
    if not pid:
        raise click.ClickException("no PID file found at " + str(PID_FILE))
    if not _pid_running(pid):
        click.echo(f"tokview was not running (stale PID {pid}); cleaning up.")
        PID_FILE.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        click.echo(f"pid {pid} already gone.")
        PID_FILE.unlink(missing_ok=True)
        return
    # Wait up to 5s for graceful shutdown
    for _ in range(50):
        if not _pid_running(pid):
            PID_FILE.unlink(missing_ok=True)
            click.echo(f"tokview stopped (pid {pid}).")
            return
        time.sleep(0.1)
    # Forced
    click.echo(f"pid {pid} did not exit; sending SIGKILL.")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    PID_FILE.unlink(missing_ok=True)


@main.command()
def status() -> None:
    """Report whether tokview is running and surface diagnostics."""
    pid = _read_pid()
    running = bool(pid and _pid_running(pid))
    tokview = load_config()
    click.echo(f"tokview {__version__}: " + ("RUNNING" if running else "STOPPED"))
    if pid:
        click.echo(f"  pid:       {pid}")
    click.echo(f"  config:    {DEFAULT_CONFIG_PATH}")
    click.echo(f"  storage:   {tokview.storage.path}")
    click.echo(f"  proxy:     http://{tokview.proxy.bind}:{tokview.proxy.port}")
    click.echo(f"  browser:   http://{tokview.dashboard.bind}:{tokview.dashboard.port}")
    if running:
        # Fetch diagnostics if we can — quick HTTP call
        try:
            import httpx

            r = httpx.get(f"http://{tokview.dashboard.bind}:{tokview.dashboard.port}/api/diagnostics", timeout=2.0)
            if r.status_code == 200:
                d = r.json()
                m = d.get("metrics", {})
                click.echo(f"  uptime:    {d.get('uptime_seconds', 0):.0f}s")
                click.echo(f"  requests:  {m.get('total_requests', 0)} total · "
                           f"{m.get('errors_24h', 0)} errors/24h · "
                           f"{m.get('estimated', 0)} estimated · "
                           f"{m.get('missing_pricing', 0)} missing-pricing")
                click.echo(f"  SSE subs:  {d.get('subscribers', 0)}")
        except Exception as e:
            click.echo(f"  diagnostics: unreachable ({e})")


@main.command()
@click.option("--session", "session_id", help="Show one session in detail.")
@click.option("--latest", is_flag=True, help="Show the most recently active session.")
@click.option("--limit", type=int, default=20, show_default=True, help="Rows per section.")
@click.option("--watch", "-w", is_flag=True, help="Refresh every 2 seconds.")
def show(session_id: str | None, latest: bool, limit: int, watch: bool) -> None:
    """Render a terminal dashboard from the local SQLite database."""
    tokview = load_config()
    if not tokview.storage.path.exists():
        raise click.ClickException(f"no database found at {tokview.storage.path}; run tokview start first")
    if limit < 1:
        raise click.ClickException("--limit must be >= 1")

    while True:
        if watch:
            click.clear()
        active_session = "latest" if latest else session_id
        click.echo(_render_cli_dashboard(tokview.storage.path, session_id=active_session, limit=limit))
        if not watch:
            return
        time.sleep(2)


@main.command()
@click.option("--tail", "-f", is_flag=True, help="Follow the log instead of printing it.")
@click.option("--lines", "-n", type=int, default=100, help="Number of lines to print (default 100).")
def logs(tail: bool, lines: int) -> None:
    """Show the tokview server logs."""
    if not LOG_FILE.exists():
        click.echo(f"no log file at {LOG_FILE}")
        return
    if tail:
        # Defer to tail(1) for follow mode — simplest reliable implementation
        subprocess.call(["tail", "-n", str(lines), "-f", str(LOG_FILE)])
    else:
        subprocess.call(["tail", "-n", str(lines), str(LOG_FILE)])


@main.command()
@click.option("--since", required=True, help="ISO date (YYYY-MM-DD) or unix ms.")
@click.option("--format", "fmt", type=click.Choice(["csv", "json"]), default="csv", help="Output format.")
def export(since: str, fmt: str) -> None:
    """Dump request rows to stdout."""
    tokview = load_config()
    # Parse since
    try:
        since_ms = int(since) if since.isdigit() else int(
            datetime.fromisoformat(since).replace(tzinfo=UTC).timestamp() * 1000
        )
    except ValueError as e:
        raise click.ClickException(f"invalid --since {since!r}: {e}")

    import sqlite3

    con = sqlite3.connect(str(tokview.storage.path))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM requests WHERE ts_ms >= ? ORDER BY ts_ms ASC",
        (since_ms,),
    ).fetchall()
    if fmt == "json":
        click.echo(json.dumps([dict(r) for r in rows], default=str))
    else:
        if not rows:
            return
        writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r[k] for k in rows[0].keys()})


@main.command()
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def reset(yes: bool) -> None:
    """Wipe the SQLite database. Stops tokview first if running."""
    tokview = load_config()
    if not yes:
        click.confirm(f"This will delete {tokview.storage.path}. Continue?", abort=True)
    pid = _read_pid()
    if pid and _pid_running(pid):
        click.echo("Stopping tokview before reset...")
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                if not _pid_running(pid):
                    break
                time.sleep(0.1)
        except ProcessLookupError:
            pass
        PID_FILE.unlink(missing_ok=True)
    for suffix in ("", "-journal", "-wal", "-shm"):
        p = Path(str(tokview.storage.path) + suffix)
        if p.exists():
            p.unlink()
    click.echo(f"deleted {tokview.storage.path} (+wal/journal sidecars).")


@main.command()
def version() -> None:
    """Print the version and exit."""
    click.echo(f"tokview {__version__}")


@main.command(name="config-path")
def config_path() -> None:
    """Print the path to the active config file."""
    click.echo(str(DEFAULT_CONFIG_PATH))


# ----- helpers -----

def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # signal 0 = check liveness only
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't own it; rare on a laptop.
        return True
    return True


def _render_cli_dashboard(db_path: Path, session_id: str | None, limit: int) -> str:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        if session_id == "latest":
            session_id = _latest_session_id(con)
        return _render_session(con, session_id, limit) if session_id else _render_overview(con, db_path, limit)
    finally:
        con.close()


def _render_overview(con: sqlite3.Connection, db_path: Path, limit: int) -> str:
    width = min(max(shutil.get_terminal_size((110, 24)).columns, 72), 140)
    compact = width < 100
    now_ms = int(time.time() * 1000)
    today_start = _utc_today_start_ms()
    week_start = now_ms - 7 * 86_400_000
    month_start = _utc_month_start_ms()
    today = _agg(con, today_start, now_ms)
    week = _agg(con, week_start, now_ms)
    month = _agg(con, month_start, now_ms)
    providers = _query(con, """
        SELECT provider, COUNT(*) requests, COALESCE(SUM(cost_usd), 0) cost_usd,
               COALESCE(SUM(input_tokens + output_tokens), 0) tokens
        FROM requests WHERE ts_ms BETWEEN ? AND ?
        GROUP BY provider ORDER BY cost_usd DESC, requests DESC LIMIT ?
    """, (month_start, now_ms, limit))
    models = _query(con, """
        SELECT model, COUNT(*) requests, COALESCE(SUM(cost_usd), 0) cost_usd,
               COALESCE(SUM(input_tokens + output_tokens), 0) tokens
        FROM requests WHERE ts_ms BETWEEN ? AND ?
        GROUP BY model ORDER BY cost_usd DESC, requests DESC LIMIT ?
    """, (month_start, now_ms, limit))
    sessions = _query(con, """
        SELECT
            r.session_id,
            COUNT(*) requests,
            COALESCE(SUM(r.cost_usd), 0) cost_usd,
            COALESCE(SUM(r.input_tokens + r.output_tokens), 0) tokens,
            COALESCE(SUM(CASE WHEN r.status_code >= 400 THEN 1 ELSE 0 END), 0) errors,
            GROUP_CONCAT(DISTINCT r.model) models,
            MAX(r.ts_ms) last_ts_ms,
            COALESCE(t.tool_tokens, 0) tool_tokens
        FROM requests r
        LEFT JOIN (
            SELECT session_id, SUM(total_tokens) tool_tokens
            FROM tool_calls
            GROUP BY session_id
        ) t ON t.session_id = r.session_id
        WHERE r.ts_ms BETWEEN ? AND ? AND r.session_id IS NOT NULL
        GROUP BY r.session_id
        ORDER BY last_ts_ms DESC
        LIMIT ?
    """, (month_start, now_ms, limit))
    calls = _query(con, """
        SELECT ts_ms, provider, model, session_id, input_tokens, output_tokens, cost_usd, status_code
        FROM requests ORDER BY ts_ms DESC LIMIT ?
    """, (limit,))
    global_tools = _query(con, """
        SELECT tool_name, COUNT(*) calls, COALESCE(SUM(arg_tokens), 0) arg_tokens,
               COALESCE(SUM(result_tokens), 0) result_tokens,
               COALESCE(SUM(total_tokens), 0) total_tokens
        FROM tool_calls
        GROUP BY tool_name
        ORDER BY total_tokens DESC
        LIMIT ?
    """, (limit,))

    out = [
        _title("tokview", width),
        f"db: {_clip(db_path, width - 4)}",
        f"now: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}    live: tokview show --watch",
        "",
        _cards([
            ("today spend", _money(today["cost_usd"]), f"{today['requests']} calls / {_num(today['tokens'])} tok"),
            ("7 day spend", _money(week["cost_usd"]), f"{week['requests']} calls / {_num(week['tokens'])} tok"),
            ("month spend", _money(month["cost_usd"]), f"{month['requests']} calls / {_num(month['tokens'])} tok"),
            ("month errors", str(month["errors"]), "failed provider calls"),
        ], width),
        "",
        _section("session spend"),
    ]
    if sessions:
        if compact:
            out.append(_table(["session", "calls", "tokens", "tools", "cost", "last"], [
                [_clip(r["session_id"] or "-", 20), str(r["requests"]), _num(r["tokens"]),
                 _num(r["tool_tokens"]), _money(r["cost_usd"]), _age(r["last_ts_ms"])]
                for r in sessions
            ], width))
        else:
            out.append(_table(["session", "calls", "tokens", "tool tok", "errors", "cost", "last", "models"], [
                [_clip(r["session_id"] or "-", 26), str(r["requests"]), _num(r["tokens"]),
                 _num(r["tool_tokens"]), str(r["errors"]), _money(r["cost_usd"]),
                 _age(r["last_ts_ms"]), _clip(r["models"] or "-", 28)]
                for r in sessions
            ], width))
        top_session = sessions[0]["session_id"]
        out.append("latest session: tokview show --latest")
        out.append(f"copy: tokview show --session {top_session}")
    else:
        out.append("  no sessions yet")

    if sessions:
        out.extend(["", _section("session request breakdowns")])
        for r in sessions[: min(3 if compact else 5, limit)]:
            sid = r["session_id"]
            out.append("")
            out.append(
                f"[session] {_clip(sid, 42)}  "
                f"{r['requests']} calls  {_num(r['tokens'])} tokens  "
                f"{_num(r['tool_tokens'])} tool tokens  {_money(r['cost_usd'])}"
            )
            session_tools = _session_tools(con, sid, 3)
            if session_tools:
                out.append("  tools: " + ", ".join(
                    f"{t['tool_name']} {_num(t['total_tokens'])}" for t in session_tools
                ))
            else:
                out.append("  tools: none recorded yet")
            recent = _session_recent_calls(con, sid, 3)
            headers = ["time", "model", "in->out", "cost", "tools"] if compact else ["time", "model", "in->out", "cost", "st", "tools"]
            rows = [
                [_time(c["ts_ms"]), _clip(c["model"] or "-", 18 if compact else 24),
                 f"{_num(c['input_tokens'])}->{_num(c['output_tokens'])}",
                 _money(c["cost_usd"]),
                 _clip(c["tools"] or "-", 20 if compact else 28)]
                for c in recent
            ]
            if not compact:
                rows = [[*row[:4], str(recent[i]["status_code"] or 200), *row[4:]] for i, row in enumerate(rows)]
            out.append(_indent(_table(headers, rows, width - 2), "  "))

    out.extend(["", _section("tool hotspots")])
    if global_tools:
        max_tokens = max(int(r["total_tokens"] or 0) for r in global_tools) or 1
        out.append(_table(["tool", "calls", "args", "results", "total", "share"], [
            [_clip(r["tool_name"], 34), str(r["calls"]), _num(r["arg_tokens"]),
             _num(r["result_tokens"]), _num(r["total_tokens"]),
             _bar(int(r["total_tokens"] or 0), max_tokens, 18)]
            for r in global_tools
        ], width))
    else:
        out.append("  no completed tool calls recorded yet")

    if not compact:
        out.extend(["", _section("provider mix")])
        out.append(_rank_table(providers, "provider", width))
        out.extend(["", _section("model mix")])
        out.append(_rank_table(models, "model", width))
    out.extend(["", _section("live tail")])
    if calls:
        if compact:
            out.append(_table(["time", "model", "in->out", "cost", "session"], [
                [_time(r["ts_ms"]), _clip(r["model"] or "-", 18),
                 f"{_num(r['input_tokens'])}->{_num(r['output_tokens'])}",
                 _money(r["cost_usd"]), _clip(r["session_id"] or "-", 16)]
                for r in calls
            ], width))
        else:
            out.append(_table(["time", "provider", "model", "in->out", "cost", "st", "session"], [
                [_time(r["ts_ms"]), r["provider"] or "-", _clip(r["model"] or "-", 28),
                 f"{_num(r['input_tokens'])}->{_num(r['output_tokens'])}", _money(r["cost_usd"]),
                 str(r["status_code"] or 200), _clip(r["session_id"] or "-", 18)]
                for r in calls
            ], width))
    else:
        out.append("  no requests yet")
    return "\n".join(out)


def _render_session(con: sqlite3.Connection, session_id: str, limit: int) -> str:
    width = min(max(shutil.get_terminal_size((110, 24)).columns, 72), 140)
    compact = width < 100
    calls = _query(con, "SELECT * FROM requests WHERE session_id = ? ORDER BY ts_ms ASC LIMIT ?", (session_id, max(limit, 500)))
    out = [_title("tokview session", width), f"session: {session_id}", ""]
    if not calls:
        out.append("no calls in this session")
        return "\n".join(out)

    cost = sum(float(r["cost_usd"] or 0) for r in calls)
    input_tokens = sum(int(r["input_tokens"] or 0) for r in calls)
    output_tokens = sum(int(r["output_tokens"] or 0) for r in calls)
    errors = sum(1 for r in calls if int(r["status_code"] or 200) >= 400)
    first = min(int(r["start_ms"] or r["ts_ms"]) for r in calls)
    last = max(int(r["ts_ms"]) for r in calls)
    out.append(_cards([
        ("calls", str(len(calls)), f"{errors} errors"),
        ("cost", _money(cost), "provider billed"),
        ("tokens", _num(input_tokens + output_tokens), f"{_num(input_tokens)} in / {_num(output_tokens)} out"),
        ("span", _duration(last - first), f"{_time(first)} -> {_time(last)}"),
    ], width))

    tools = _session_tools(con, session_id, limit)
    out.extend(["", _section("tool token attribution")])
    if tools:
        max_tokens = max(int(r["total_tokens"] or 0) for r in tools) or 1
        out.append(_table(["tool", "calls", "args", "results", "total", "share"], [
            [_clip(r["tool_name"], 34), str(r["calls"]), _num(r["arg_tokens"]),
             _num(r["result_tokens"]), _num(r["total_tokens"]),
             _bar(int(r["total_tokens"] or 0), max_tokens, 18)]
            for r in tools
        ], width))
    else:
        out.append("  no completed tool calls recorded yet")

    out.extend(["", _section("request timeline")])
    timeline = _query(con, """
        SELECT
            r.ts_ms, r.model, r.input_tokens, r.output_tokens, r.latency_ms, r.ttft_ms,
            r.cost_usd, r.status_code, COALESCE(t.tools, '') tools
        FROM requests r
        LEFT JOIN (
            SELECT request_id, GROUP_CONCAT(tool_name || ':' || total_tokens, ', ') tools
            FROM tool_calls
            GROUP BY request_id
        ) t ON t.request_id = r.request_id
        WHERE r.session_id = ?
        ORDER BY r.ts_ms ASC
        LIMIT ?
    """, (session_id, limit))
    if compact:
        out.append(_table(["time", "model", "in->out", "cost", "tools"], [
            [_time(r["ts_ms"]), _clip(r["model"] or "-", 18),
             f"{_num(r['input_tokens'])}->{_num(r['output_tokens'])}",
             _money(r["cost_usd"]), _clip(r["tools"] or "-", 18)]
            for r in timeline
        ], width))
    else:
        out.append(_table(["time", "model", "in->out", "latency", "ttft", "cost", "st", "tools"], [
            [_time(r["ts_ms"]), _clip(r["model"] or "-", 24),
             f"{_num(r['input_tokens'])}->{_num(r['output_tokens'])}",
             _duration(r["latency_ms"]), _duration(r["ttft_ms"]),
             _money(r["cost_usd"]), str(r["status_code"] or 200), _clip(r["tools"] or "-", 26)]
            for r in timeline
        ], width))
    return "\n".join(out)


def _agg(con: sqlite3.Connection, since_ms: int, until_ms: int) -> dict[str, int | float]:
    row = con.execute("""
        SELECT COALESCE(SUM(cost_usd), 0) cost_usd, COUNT(*) requests,
               COALESCE(SUM(input_tokens + output_tokens), 0) tokens,
               COALESCE(SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END), 0) errors
        FROM requests WHERE ts_ms BETWEEN ? AND ?
    """, (since_ms, until_ms)).fetchone()
    return dict(row) if row else {"cost_usd": 0.0, "requests": 0, "tokens": 0, "errors": 0}


def _query(con: sqlite3.Connection, sql: str, args: tuple[object, ...]) -> list[sqlite3.Row]:
    return list(con.execute(sql, args).fetchall())


def _latest_session_id(con: sqlite3.Connection) -> str | None:
    row = con.execute("""
        SELECT session_id
        FROM requests
        WHERE session_id IS NOT NULL
        GROUP BY session_id
        ORDER BY MAX(ts_ms) DESC
        LIMIT 1
    """).fetchone()
    return None if row is None else str(row["session_id"])


def _session_tools(con: sqlite3.Connection, session_id: str, limit: int) -> list[sqlite3.Row]:
    return _query(con, """
        SELECT tool_name, COUNT(*) calls, COALESCE(SUM(arg_tokens), 0) arg_tokens,
               COALESCE(SUM(result_tokens), 0) result_tokens,
               COALESCE(SUM(total_tokens), 0) total_tokens
        FROM tool_calls WHERE session_id = ?
        GROUP BY tool_name ORDER BY total_tokens DESC LIMIT ?
    """, (session_id, limit))


def _session_recent_calls(con: sqlite3.Connection, session_id: str, limit: int) -> list[sqlite3.Row]:
    return _query(con, """
        SELECT
            r.ts_ms, r.model, r.input_tokens, r.output_tokens, r.cost_usd, r.status_code,
            COALESCE(t.tools, '') tools
        FROM requests r
        LEFT JOIN (
            SELECT request_id, GROUP_CONCAT(tool_name || ':' || total_tokens, ', ') tools
            FROM tool_calls
            GROUP BY request_id
        ) t ON t.request_id = r.request_id
        WHERE r.session_id = ?
        ORDER BY r.ts_ms DESC
        LIMIT ?
    """, (session_id, limit))


def _utc_today_start_ms() -> int:
    now = datetime.now(UTC)
    return int(datetime(now.year, now.month, now.day, tzinfo=UTC).timestamp() * 1000)


def _utc_month_start_ms() -> int:
    now = datetime.now(UTC)
    return int(datetime(now.year, now.month, 1, tzinfo=UTC).timestamp() * 1000)


def _title(text: str, width: int) -> str:
    return f" {text} ".center(width, "=")


def _section(text: str) -> str:
    return text.upper()


def _cards(cards: list[tuple[str, str, str]], width: int) -> str:
    card_w = max(18, min(28, (width - 3 * (len(cards) - 1)) // len(cards)))
    return "\n".join(
        "   ".join(part.ljust(card_w) for part in row)
        for row in zip(*cards, strict=True)
    )


def _rank_table(rows: list[sqlite3.Row], label_col: str, width: int) -> str:
    if not rows:
        return "  no data yet"
    max_cost = max(float(r["cost_usd"] or 0) for r in rows) or 1.0
    return _table([label_col, "calls", "tokens", "cost", "share"], [
        [_clip(r[label_col] or "-", 40), str(r["requests"]), _num(r["tokens"]),
         _money(r["cost_usd"]), _bar(float(r["cost_usd"] or 0), max_cost, 18)]
        for r in rows
    ], width)


def _table(headers: list[str], rows: list[list[str]], width: int) -> str:
    if not rows:
        return ""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))
    total = sum(col_widths) + 2 * (len(headers) - 1)
    if total > width:
        col_widths[0] = max(12, col_widths[0] - (total - width))
    fmt = "  ".join("{:<" + str(w) + "}" for w in col_widths)
    out = [fmt.format(*[_clip(h, col_widths[i]) for i, h in enumerate(headers)])]
    out.append(fmt.format(*["-" * w for w in col_widths]))
    for row in rows:
        out.append(fmt.format(*[_clip(str(row[i]), col_widths[i]) for i in range(len(headers))]))
    return "\n".join(out)


def _bar(value: float, max_value: float, width: int) -> str:
    filled = 0 if max_value <= 0 else round((value / max_value) * width)
    filled = max(0, min(width, filled))
    return "#" * filled + "." * (width - filled)


def _clip(value: object, width: int) -> str:
    text = str(value)
    return text if len(text) <= width else text[: max(0, width - 1)] + "~"


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def _money(value: object) -> str:
    n = float(value or 0)
    if 0 < abs(n) < 0.0001:
        return f"${n:.6f}"
    return f"${n:.4f}"


def _num(value: object) -> str:
    return f"{int(value or 0):,}"


def _time(ms: object) -> str:
    return "-" if ms is None else datetime.fromtimestamp(int(ms) / 1000).strftime("%H:%M:%S")


def _age(ms: object) -> str:
    if ms is None:
        return "-"
    return _duration(max(0, int(time.time() * 1000) - int(ms))) + " ago"


def _duration(ms: object) -> str:
    if ms is None:
        return "-"
    n = int(ms)
    if n < 1000:
        return f"{n}ms"
    seconds = n // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h"


def _banner_running(tokview: TokviewConfig, pid: int) -> str:
    width = 74
    lines = [
        f"tokview v{__version__}",
        "",
        f"  started in background (pid {pid})",
        "  stop with:  tokview stop",
        "  status:     tokview status",
    ]
    bar = "+" + "-" * (width - 2) + "+"
    body = "\n".join("| " + ln.ljust(width - 4) + " |" for ln in lines)
    return f"{bar}\n{body}\n{bar}"


def _print_banner(tokview: TokviewConfig) -> None:
    proxy_url = f"http://{tokview.proxy.bind}:{tokview.proxy.port}"
    dash_url = f"http://{tokview.dashboard.bind}:{tokview.dashboard.port}"
    width = 74
    lines = [
        "tokview v" + __version__,
        "",
        f"  Proxy     {proxy_url}",
        f"  Dashboard {dash_url}",
        "",
        "  Point your apps at the proxy:",
        f"    Anthropic:    export ANTHROPIC_BASE_URL={proxy_url}",
        f"    OpenAI:       export OPENAI_BASE_URL={proxy_url}/v1",
        f"    Gemini:       export GOOGLE_BASE_URL={proxy_url}",
        "    Claude Code:  ANTHROPIC_BASE_URL=" + proxy_url + " claude",
        "",
        "  Stop with Ctrl-C.",
    ]
    bar = "+" + "-" * (width - 2) + "+"
    click.echo(bar)
    for line in lines:
        click.echo("| " + line.ljust(width - 4) + " |")
    click.echo(bar)


if __name__ == "__main__":
    main()
    sys.exit(0)

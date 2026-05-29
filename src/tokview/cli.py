"""`tokview` command-line interface.

Commands:
    tokview start [--foreground] [--allow-remote]
    tokview stop
    tokview status
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
import signal
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
    """Start the proxy and dashboard."""
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
        dash_url = f"http://{tokview.dashboard.bind}:{tokview.dashboard.port}"
        click.echo(_banner_running(tokview, child.pid))
        click.echo(f"\nLogs: {LOG_FILE}\nProxy: {proxy_url}\nDashboard: {dash_url}")
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
    click.echo(f"  dashboard: http://{tokview.dashboard.bind}:{tokview.dashboard.port}")
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

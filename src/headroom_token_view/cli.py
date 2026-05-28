"""`htv` command-line interface.

In iter 1: `htv start` (foreground) and `htv version`. Daemonization,
status, stop, logs, export, reset land in iter 8.
"""
from __future__ import annotations

import asyncio
import sys

import click

from . import __version__
from .config import DEFAULT_CONFIG_PATH, HtvConfig, load as load_config
from .server import serve

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


@click.group(help="Headroom Token View — drop-in LLM proxy and cost dashboard.")
@click.version_option(version=__version__, prog_name="htv")
def main() -> None:
    """Command group."""


@main.command()
@click.option(
    "--allow-remote",
    is_flag=True,
    default=False,
    help="Allow non-loopback bind addresses. v1 has NO authentication — use only if you accept the risk.",
)
def start(allow_remote: bool) -> None:
    """Start the proxy and dashboard (foreground in v1)."""
    htv = load_config()

    # Trust boundary per spec §6.4: non-loopback bind requires both the config value
    # and the --allow-remote flag.
    non_loopback = htv.proxy.bind not in LOOPBACK or htv.dashboard.bind not in LOOPBACK
    if non_loopback and not allow_remote:
        raise click.ClickException(
            "Config requests a non-loopback bind (proxy.bind=%r, dashboard.bind=%r) "
            "but --allow-remote was not passed.\n"
            "v1 has no authentication; team deploys belong on the Postgres+Docker path.\n"
            "If you accept the risk, run: htv start --allow-remote"
            % (htv.proxy.bind, htv.dashboard.bind)
        )

    _print_banner(htv)
    try:
        asyncio.run(serve(htv))
    except KeyboardInterrupt:
        click.echo("\nstopped.")


@main.command()
def version() -> None:
    """Print the version and exit."""
    click.echo(f"headroom-token-view {__version__}")


@main.command(name="config-path")
def config_path() -> None:
    """Print the path to the active config file."""
    click.echo(str(DEFAULT_CONFIG_PATH))


def _print_banner(htv: HtvConfig) -> None:
    proxy_url = f"http://{htv.proxy.bind}:{htv.proxy.port}"
    dash_url = f"http://{htv.dashboard.bind}:{htv.dashboard.port}"
    width = 74
    lines = [
        "Headroom Token View v" + __version__,
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

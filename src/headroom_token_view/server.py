"""Orchestrate the LiteLLM proxy and the HTV dashboard backend in one process.

Both services share the asyncio event loop via `uvicorn.Server` + `asyncio.gather`.
The proxy is LiteLLM's FastAPI app; the dashboard is ours. They listen on
separate ports so that exposing the dashboard never exposes the proxy and
vice-versa.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

import uvicorn

from .config import DEFAULT_DIR, HtvConfig
from .litellm_config import write as write_litellm_config

logger = logging.getLogger(__name__)


async def serve(htv: HtvConfig) -> None:
    """Run LiteLLM proxy + HTV dashboard backend in one process, until SIGINT/SIGTERM."""
    # Generate and point LiteLLM at our generated config BEFORE importing the proxy app
    litellm_config_path = DEFAULT_DIR / "litellm-config.yaml"
    write_litellm_config(htv, litellm_config_path)
    os.environ["CONFIG_FILE_PATH"] = str(litellm_config_path)

    # SECURITY: Use the cost map bundled in the pinned LiteLLM wheel — do NOT
    # fetch model_prices_and_context_window.json from GitHub at runtime.
    # That auto-fetch is the vector for the 2026-01-27 cost-map incident.
    # HTV will add its own SHA-256-verified refresh in a later iteration
    # (per spec §8); until then, prices are pinned to the LiteLLM release.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

    # Imported lazily so the CONFIG_FILE_PATH and LITELLM_LOCAL_MODEL_COST_MAP
    # env vars are in place at module load.
    from litellm.proxy.proxy_server import app as litellm_app  # noqa: PLC0415

    from .dashboard import build_app  # noqa: PLC0415

    dashboard_app = build_app()

    proxy_cfg = uvicorn.Config(
        litellm_app,
        host=htv.proxy.bind,
        port=htv.proxy.port,
        log_level="info",
        access_log=False,
    )
    dash_cfg = uvicorn.Config(
        dashboard_app,
        host=htv.dashboard.bind,
        port=htv.dashboard.port,
        log_level="info",
        access_log=False,
    )

    proxy_server = uvicorn.Server(proxy_cfg)
    dash_server = uvicorn.Server(dash_cfg)

    loop = asyncio.get_running_loop()

    def request_shutdown() -> None:
        logger.info("shutdown requested; stopping proxy and dashboard")
        proxy_server.should_exit = True
        dash_server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            # Windows fallback — uvicorn handles SIGINT itself
            pass

    await asyncio.gather(
        proxy_server.serve(),
        dash_server.serve(),
    )


def litellm_config_path() -> Path:
    """Where the generated LiteLLM config lives."""
    return DEFAULT_DIR / "litellm-config.yaml"

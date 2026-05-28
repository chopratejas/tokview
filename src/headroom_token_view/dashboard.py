"""FastAPI app for the Headroom Token View dashboard backend.

In iter 1 this only exposes /api/health so we can confirm the dashboard
process is alive. Subsequent iterations add /api/summary, /api/calls,
/api/sessions, /api/providers, /api/events (SSE) and serve the embedded
SvelteKit SPA at /.
"""
from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__


def build_app() -> FastAPI:
    started_at = time.time()
    app = FastAPI(
        title="Headroom Token View",
        version=__version__,
        docs_url=None,  # no Swagger UI by default; spec is dashboard-first
        redoc_url=None,
    )

    @app.get("/api/health")
    def health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "uptime_seconds": round(time.time() - started_at, 1),
            }
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        # Placeholder until iter 5 swaps in the SvelteKit build.
        return HTMLResponse(
            """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Headroom Token View</title>
  <style>
    body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, sans-serif; padding: 2em; max-width: 720px; margin: auto; color: #1a1a1a; background: #fafafa; }
    h1 { margin-top: 0; font-weight: 600; }
    code { background: #eef; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }
    .ok { color: #0a7d32; }
  </style>
</head>
<body>
  <h1>Headroom Token View <span class="ok">●</span></h1>
  <p>Proxy and dashboard are running. Dashboard UI lands in iteration 5.</p>
  <p>API health: <code><a href="/api/health">/api/health</a></code></p>
  <p>Point your apps at the proxy:</p>
  <pre>export ANTHROPIC_BASE_URL=http://localhost:4000
export OPENAI_BASE_URL=http://localhost:4000/v1
export GOOGLE_BASE_URL=http://localhost:4000</pre>
</body>
</html>"""
        )

    return app

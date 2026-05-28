"""Custom Hatchling build hook.

Makes the SvelteKit `web/build/` force-include resilient when the SPA hasn't
been built yet. This matters for:

- `pip install -e .` by contributors who don't have Node installed
- CI jobs that lint/test Python but don't build the SPA
- Reproducible sdist -> wheel-from-sdist flows when the upstream skipped npm

Wheel releases for PyPI should always have the SPA built first; the
publish.yml workflow runs `npm install && npm run build` before
`python -m build`. This hook is a *fallback* so dev installs don't
break — it writes a tiny placeholder that the runtime serves with a
"Build the SPA with `npm run build`" message.
"""
from __future__ import annotations

import os
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


_PLACEHOLDER_HTML = """<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"/><title>Headroom Token View</title></head>
  <body style="font:14px system-ui;padding:2em;color:#1a1a1a;background:#fafafa">
    <h2>Headroom Token View</h2>
    <p>The SvelteKit dashboard wasn't included in this install
       (you're either running an editable install without Node,
       or someone built the wheel without running <code>npm run build</code>).</p>
    <p>To get the full dashboard, in the source tree:</p>
    <pre>cd web &amp;&amp; npm install &amp;&amp; npm run build</pre>
    <p>The proxy itself is functioning at
       <a href="http://localhost:4000">localhost:4000</a> —
       the dashboard is the only thing affected.</p>
    <p><a href="/api/health">/api/health</a></p>
  </body>
</html>
"""


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "spa-stub"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """Ensure `web/build/index.html` exists before Hatchling enforces force-include."""
        web_build = os.path.join(self.root, "web", "build")
        index_html = os.path.join(web_build, "index.html")
        if not os.path.isdir(web_build):
            os.makedirs(web_build, exist_ok=True)
        if not os.path.exists(index_html):
            with open(index_html, "w", encoding="utf-8") as fh:
                fh.write(_PLACEHOLDER_HTML)

# Third-party Notices

`tokview` bundles or depends on a number of open-source projects. Their licenses and copyrights are preserved per the terms of their respective licenses; this file enumerates them.

## Bundled at install time

These are installed alongside `tokview` when you `pipx install` it. Their full license texts are preserved in the wheel.

### Proxy engine
- **[LiteLLM](https://github.com/BerriAI/litellm)** by BerriAI — MIT license. Powers the underlying provider-routing, cost calculation, and streaming-usage parsing.

### Web stack
- **[FastAPI](https://fastapi.tiangolo.com/)** — MIT.
- **[Uvicorn](https://www.uvicorn.org/)** — BSD-3-Clause.
- **[Starlette](https://www.starlette.io/)** (via FastAPI) — BSD-3-Clause.
- **[Pydantic](https://github.com/pydantic/pydantic)** — MIT.
- **[Click](https://click.palletsprojects.com/)** — BSD-3-Clause.
- **[structlog](https://www.structlog.org/)** — MIT / Apache-2.0.
- **[aiosqlite](https://github.com/omnilib/aiosqlite)** — MIT.
- **[httpx](https://www.python-httpx.org/)** — BSD-3-Clause.
- **[PyYAML](https://pyyaml.org/)** — MIT.

### Dashboard SPA (bundled into the wheel)
- **[SvelteKit](https://kit.svelte.dev/)** + **[Svelte](https://svelte.dev/)** — MIT.
- **[Apache ECharts](https://echarts.apache.org/)** — Apache-2.0.
- **[Vite](https://vitejs.dev/)** (build only) — MIT.

## Transitive dependencies

Each of the above brings further transitive dependencies (the full graph is large and dominated by the LiteLLM dependency tree — Anthropic, OpenAI, and Google SDKs, AWS / Azure SDKs, OpenTelemetry, Redis client, etc.). All are licensed under permissive open-source licenses (MIT / Apache-2.0 / BSD); a complete enumeration with versions is in the wheel's `METADATA` file:

```bash
pip show -f tokview
```

If a license incompatibility is discovered, please open an issue or email `chopratejas@gmail.com`.

## Trademarks

"Claude" and "Anthropic" are trademarks of Anthropic, PBC. "GPT", "ChatGPT", and "OpenAI" are trademarks of OpenAI, Inc. "Gemini" and "Google" are trademarks of Google LLC. References in this project are nominative — `tokview` is not affiliated with, endorsed by, or sponsored by any of these companies.

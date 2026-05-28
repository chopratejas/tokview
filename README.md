# headroom-token-view

A small, local token viewer for LLM API calls.

It runs a tiny proxy on your laptop. Point your apps at it (one env var) and it shows the exact token usage and cost of every call you make to Claude, OpenAI, Gemini, and anything else [LiteLLM](https://github.com/BerriAI/litellm) supports, in a simple dashboard.

That's it. No accounts. No cloud. No Docker. One `pipx install`.

[![CI](https://github.com/chopratejas/headroom-token-view/actions/workflows/ci.yml/badge.svg)](https://github.com/chopratejas/headroom-token-view/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/headroom-token-view.svg)](https://pypi.org/project/headroom-token-view/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

## Quick start

```bash
pipx install headroom-token-view
htv start
```

You'll see:

```
+--------------------------------------------------------------------------+
| Headroom Token View v0.0.1                                               |
|                                                                          |
|   started in background (pid 12345)                                      |
+--------------------------------------------------------------------------+

Logs: /Users/you/.headroom-token-view/htv.log
Proxy: http://127.0.0.1:4000
Dashboard: http://127.0.0.1:3000
```

Point any app at the proxy:

```bash
export ANTHROPIC_BASE_URL=http://localhost:4000
export OPENAI_BASE_URL=http://localhost:4000/v1
export GOOGLE_BASE_URL=http://localhost:4000
```

Open the dashboard: <http://localhost:3000>.

Now make calls as usual (Anthropic SDK, OpenAI SDK, `curl`, Claude Code, whatever). They flow through the proxy. The dashboard fills in within milliseconds.

### Track Claude Code itself

```bash
ANTHROPIC_BASE_URL=http://localhost:4000 claude
```

Every Claude Code interaction lands in the dashboard.

## What it shows

- $ spent today / this week / month-to-date
- Per-provider, per-model, per-session, per-tag breakdowns
- Cache hit visibility (Anthropic prompt caching, OpenAI cached tokens, Gemini context cache)
- Reasoning-token costs (o-series, Claude extended thinking)
- A live tail of recent calls with status + latency
- Real-time updates via SSE — no refresh needed

## What it doesn't do (intentionally)

- No team / multi-user features. Single user, localhost only.
- No virtual API keys. Your real provider keys are read from env vars and forwarded straight to the provider.
- No alerting / Slack integration. Not yet.
- No data leaves your machine. Everything in `~/.headroom-token-view/db.sqlite`.
- No prompt content stored by default. (Opt-in with redaction; see Privacy below.)

Want any of these? Open an issue. The architecture is designed to evolve into a Postgres + Docker + auth setup later — see the design spec for the "🅑 path".

## How it works

```
Your apps ──► headroom-token-view Proxy ──► Provider APIs
                       │
                       ├─ writes a row → SQLite
                       └─ pushes a spend event → SSE → Dashboard
```

The proxy is [LiteLLM](https://github.com/BerriAI/litellm) underneath, with a thin layer that captures each call's cost (from the provider's own `usage` field — not a tokenizer estimate) and exposes a small REST API + SSE for the dashboard.

The dashboard is SvelteKit + ECharts, bundled into the Python wheel.

Response bytes flow through the proxy unchanged. SDKs don't know they're talking to a proxy — your code doesn't change.

## CLI

```
htv start [-f]            start the proxy + dashboard (daemonizes; -f for foreground)
htv stop                  graceful SIGTERM
htv status                pid, uptime, request counts, errors, diagnostics
htv logs [-f] [-n N]      tail the server log
htv export --since DATE   csv/json dump of all calls since DATE
htv reset                 wipe the SQLite database (with confirmation)
htv version
htv config-path
```

## Configuration

`~/.headroom-token-view/config.yaml` is auto-generated on first start. Defaults are localhost-only on ports 3000 / 4000.

```yaml
proxy:        { port: 4000, bind: 127.0.0.1 }
dashboard:    { port: 3000, bind: 127.0.0.1 }
storage:      { path: ~/.headroom-token-view/db.sqlite }
retention:    { days: 90 }
capture:      { prompts: false, responses: false }
```

Provider API keys come from environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`). HTV never reads or persists them.

## Privacy

Default: only token counts + cost + metadata. **No prompt text. No response text.**

If you want full request/response logging, enable it in the config — regex-based redaction runs *before* persistence, so the DB never holds raw secrets:

```yaml
capture:
  prompts: true
  responses: true
  redact_patterns:
    - '(sk|pk)-[A-Za-z0-9]{20,}'
    - '[\w.+-]+@[\w-]+\.[\w.-]+'
```

## Security stance

Two things worth knowing:

1. LiteLLM is soft-pinned `>=1.86.1,<2.0.0`. Patches and new-model support arrive automatically on `pipx upgrade`. A future LiteLLM 2.0 major (allowed to break things) is held back until an HTV release verifies it.
2. The LiteLLM runtime cost-map fetch is **disabled**. Prices come from the pinned wheel, not a GitHub fetch — closing the vector for the [2026-01-27 cost-map incident](https://docs.litellm.ai/blog/model-cost-map-incident).

Full threat model in [SECURITY.md](SECURITY.md).

## Status

`v0.0.x` — alpha. Single-user laptop tool. Works against Claude, OpenAI, Gemini and the rest of LiteLLM's catalog.

Roadmap is short and lives in [CHANGELOG.md](CHANGELOG.md). Highlights for the next milestones:
- HTV-managed cost-map refresh with hash verification
- `htv test-providers` smoke command (a $0.001 token per provider)
- Optional Postgres backend for multi-user use

## Contributing

PRs welcome. The loop is:

```bash
pip install -e ".[dev]"
ruff check src tests
pytest -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Acknowledgments

- [LiteLLM](https://github.com/BerriAI/litellm) does the actual heavy lifting: provider coverage, cost calculation, streaming-usage parsing. HTV is a thin layer on top.
- [FastAPI](https://fastapi.tiangolo.com/), [SvelteKit](https://svelte.dev/), [Apache ECharts](https://echarts.apache.org/), and [SQLite](https://www.sqlite.org/).

## License

[MIT](LICENSE). © 2026 Tejas Chopra.

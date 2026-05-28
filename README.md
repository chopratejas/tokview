# Headroom Token View

> A drop-in LLM proxy + branded dashboard that tracks **exact** token usage and cost across Claude, OpenAI, Gemini, and every provider [LiteLLM](https://github.com/BerriAI/litellm) supports.

[![CI](https://github.com/chopratejas/headroom-token-view/actions/workflows/ci.yml/badge.svg)](https://github.com/chopratejas/headroom-token-view/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/headroom-token-view.svg)](https://pypi.org/project/headroom-token-view/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

---

```
+--------------------------------------------------------------------------+
| Headroom Token View                                              ●LIVE  |
+--------------------------------------------------------------------------+
|  TODAY            THIS WEEK         MTD              CACHE READS        |
|  $4.12 · 87 calls $42.18 · 1,204    $128.30          18,432 tokens      |
|                                                                          |
|  Cost / minute · last hour                                               |
|  ▁▂▃▅▇█▇▅▃▂▁▁▂▃▅▇█▇▅▃▁▂▃▅▇█▇▅▃▂                                          |
|                                                                          |
|  By provider           By model             By session                   |
|  Claude    $28.40 ███  claude-3-7-sonnet    claude-code-…  $35.42 ▌      |
|  OpenAI    $11.05 ██   gpt-4o               session-a3f1    $4.20 ▎      |
|  Gemini     $2.73 █    gemini-2.5-pro       session-bb09    $2.18 ▏      |
+--------------------------------------------------------------------------+
```

## What you get

- 🪟 **One install command.** `pipx install headroom-token-view && htv start`. No Docker, no Postgres, no Redis.
- 🎯 **Exact costs**, not estimates. HTV reads the provider's own `usage` object (Anthropic `cache_creation_input_tokens` / `cache_read_input_tokens`, OpenAI `prompt_tokens_details.cached_tokens`, Gemini `usageMetadata`, o-series + extended-thinking reasoning tokens) and applies LiteLLM's pricing map.
- 🔌 **Drop-in.** Change one env var (`ANTHROPIC_BASE_URL=http://localhost:4000`) and every existing SDK / Claude Code / curl call routes through the proxy. Response bytes are identical to what the provider returned.
- 📡 **Real-time dashboard.** SvelteKit + ECharts; live cost ticker via SSE; breakdowns by provider, model, session, tag, user-agent; live request tail.
- 💾 **Persistent.** SQLite at `~/.headroom-token-view/db.sqlite`; survives restarts; CSV / JSON export.
- 🔒 **Hardened against the LiteLLM cost-map incident.** Runtime fetch of `model_prices_and_context_window.json` is disabled; prices come from the pinned wheel.

## Quick start

```bash
# 1. Install
pipx install headroom-token-view

# 2. Start the proxy + dashboard (daemonized)
htv start
#  ✓ Proxy     http://localhost:4000
#  ✓ Dashboard http://localhost:3000

# 3. Point any app at the proxy. That's it.
export ANTHROPIC_BASE_URL=http://localhost:4000
export OPENAI_BASE_URL=http://localhost:4000/v1
export GOOGLE_BASE_URL=http://localhost:4000

# 4. Open the dashboard
open http://localhost:3000
```

That's the whole product. Your apps don't change. The dashboard fills in within milliseconds of every call.

### Track Claude Code itself

```bash
ANTHROPIC_BASE_URL=http://localhost:4000 claude
```

Every Claude Code interaction now lands in the dashboard.

## How it works

```
   Your apps  ┐
  Claude Code ├─► Headroom Token View Proxy (:4000) ─► Provider APIs
   OpenAI SDK │       │
   Gemini SDK │       ├─ tees stream, parses provider 'usage'
   curl       ┘       ├─ writes spend row → SQLite
                      └─ pushes spend event → SSE
                                                    ▲
                                                    │
                                    Dashboard SPA (:3000)
                                    live ticker · breakdowns ·
                                    per-session drill-down
```

- **Transparent forwarding.** The proxy never modifies bytes going to the client. SDKs don't know they're talking to a proxy.
- **Off the hot path.** Cost calculation, DB writes, and pub/sub all happen *after* `[DONE]`. No latency added to the user's request.
- **Streaming-safe.** Handles Anthropic `message_delta`, OpenAI `stream_options.include_usage`, Gemini `usageMetadata`. Drains every stream to `[DONE]` so even vLLM-style trailing-usage chunks are captured.

## Architecture choices (the short version)

| Concern | v1 (`pipx`) | Team / SaaS (later) |
|---|---|---|
| Storage | SQLite (WAL) | Postgres |
| Process | One Python process | docker-compose |
| Auth | None (localhost only) | Magic-link / SSO |
| Provider keys | Passthrough from env | Virtual keys (`htv-sk-…`) |
| Tenancy | Single user | Multi-team |

Schema is forward-compatible (`team_id` nullable; no migration on graduation).

The full spec is at [`docs/superpowers/specs/2026-05-27-headroom-token-view-design.md`](docs/superpowers/specs/2026-05-27-headroom-token-view-design.md).

## CLI

```
htv start [--foreground/-f] [--allow-remote]   start the proxy + dashboard
htv stop                                       graceful SIGTERM, SIGKILL fallback
htv status                                     pid, uptime, request counts, errors
htv logs [-f] [-n LINES]                       tail the server log
htv export --since YYYY-MM-DD                  dump requests to stdout (csv | json)
htv reset [--yes]                              wipe the SQLite database
htv version
htv config-path
```

## Configuration

First start writes a default `~/.headroom-token-view/config.yaml`:

```yaml
proxy:        { port: 4000, bind: 127.0.0.1 }
dashboard:    { port: 3000, bind: 127.0.0.1 }
storage:      { path: ~/.headroom-token-view/db.sqlite }
litellm:
  always_include_stream_usage: true     # critical for OpenAI streaming usage
pricing:
  refresh_interval_hours: 24
  alert_on_zero_cost: true              # warn when tokens > 0 and cost = $0
  fallback_to_last_known_good: true
retention:    { days: 90 }
capture:      { prompts: false, responses: false }   # opt-in; see §Privacy
```

Provider API keys are read from environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`) by LiteLLM. **HTV never persists API keys.**

## Privacy

By default HTV stores only **token counts + cost + metadata** — no prompt content, no response content.

Prompts can be **opt-in captured** with regex-based redaction applied *before* persistence:

```yaml
capture:
  prompts: true
  responses: true
  redact_patterns:
    - '(sk|pk)-[A-Za-z0-9]{20,}'
    - '[\w.+-]+@[\w-]+\.[\w.-]+'
  max_chars_per_field: 8192
```

When enabled, redacted spans are replaced with `[REDACTED:<rule>]` *before* the row reaches SQLite. The DB never holds the raw secret.

## Security

See [SECURITY.md](SECURITY.md) for the full stance and how to report vulnerabilities.

The headlines:

1. **LiteLLM soft-pinned** at `>=1.86.1,<2.0.0`. Patches and minors of LiteLLM 1.x land on `pipx upgrade` — you get new models automatically. A future 2.0 major (allowed to break things) is held back until HTV has verified it.
2. **Cost map frozen at install time** via `LITELLM_LOCAL_MODEL_COST_MAP=True`. HTV does **not** fetch `model_prices_and_context_window.json` from `main` at runtime — the vector for the [2026-01-27 cost-map incident](https://docs.litellm.ai/blog/model-cost-map-incident).
3. **Default bind is `127.0.0.1`.** Non-loopback binds require an explicit `htv start --allow-remote` flag *and* the corresponding config value. v1 has no authentication; team deployments belong on the Postgres + Docker graduation path.

## Status & roadmap

**v0.0.x** — solo laptop. Drop-in proxy, exact cost tracking, branded dashboard.

**v0.1.x** (planned) —
- Webhook output (Slack / generic HTTP) for spend thresholds
- HTV-owned SHA-256-verified cost-map refresh (currently frozen until HTV release)
- `htv test-providers` — smoke each configured provider end-to-end with a $0.001 token
- Errors / Health tabs in the SvelteKit SPA

**v1.0** (the "🅑 path" in the spec) —
- Postgres backend (drop-in via `DATABASE_URL`)
- docker-compose deploy
- Magic-link / SSO auth on the dashboard
- LiteLLM-backed virtual keys (`htv-sk-…`) with per-user / per-team budgets
- Multi-tenant rollups

Open a [discussion](../../discussions) if you want to weigh in on priority.

## Contributing

Issues and PRs welcome. Run the full test loop before submitting:

```bash
pip install -e ".[dev]"
ruff check src tests
pytest -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the longer guide.

## Acknowledgments

Headroom Token View stands on the shoulders of a few:

- [**LiteLLM**](https://github.com/BerriAI/litellm) — the proxy engine. HTV is a thin layer; the cost math, streaming-usage parsing, and 100+ provider support are LiteLLM's.
- [**FastAPI**](https://fastapi.tiangolo.com/) + [**Uvicorn**](https://www.uvicorn.org/)
- [**SvelteKit**](https://svelte.dev/) + [**Apache ECharts**](https://echarts.apache.org/) for the dashboard
- [**SQLite**](https://www.sqlite.org/) for being so well-engineered that "single Python process + WAL" is a viable production design

## License

[MIT](LICENSE) © 2026 Tejas Chopra

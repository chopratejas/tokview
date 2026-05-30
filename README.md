# tokview

> **See exactly where your LLM tokens go — down to the individual tool call.**

[![CI](https://github.com/chopratejas/tokview/actions/workflows/ci.yml/badge.svg)](https://github.com/chopratejas/tokview/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/token-viewer.svg)](https://pypi.org/project/token-viewer/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11--3.13-blue.svg)](https://www.python.org/downloads/)

A tiny **local** proxy plus a **terminal dashboard**. Point any app at the proxy with one env var; `tokview show` then breaks your token spend down by model, by session, and — uniquely — by *tool call*. The browser dashboard is optional; the terminal TUI is the primary workflow and has no Node/npm dependency.

<!--
  Real terminal screenshot goes here. Once captured, drop the image in docs/
  and uncomment the line below (it can sit above or replace the sketch):
  ![tokview terminal dashboard](docs/tui.png)
-->

Here's the per-tool breakdown it surfaces directly in your terminal:

```text
  tokview
  today spend $0.42      7 day spend $3.18      month spend $9.74

  SESSION SPEND
  session             calls  tokens   tool tok  errors  cost
  claude-code-7b3a    47     2.1M     1.9M      0       $2.83

  SESSION REQUEST BREAKDOWNS
  [session] claude-code-7b3a  47 calls  2.1M tokens  1.9M tool tokens
    tools: Read 1,240k, mcp__github__search 410k, Bash 180k
    time      model          in->out      cost    tools
    14:21:03  claude-opus    180k->2.1k   $0.23   Read:124000, Bash:18000

  TOOL HOTSPOTS
  tool                  calls  args   results  total
  Read                  12     4k     1,236k   1,240k
  mcp__github__search   5      9k     401k     410k
  Bash                  8      2k     178k     180k
```

Most tools just tell you a call used 180k tokens. tokview tells you *which tool* spent them — and catches the dominant hidden agent cost: a big tool result (a `Read`, an MCP dump) re-billed as input on every later turn. Tracing platforms can show this only if you wrap your code in their SDK; tokview gets it from one env var, for any app or CLI you can point at a URL — even Claude Code. No account, no cloud, no Docker.

## Quick start

```bash
pipx install token-viewer    # the command it installs is `tokview`
tokview start
```

tokview currently supports Python 3.11 through 3.13. If `pipx` is configured to
use a newer interpreter, install with a supported one:

```bash
pipx install --python python3.13 token-viewer
```

Prefer [uv](https://docs.astral.sh/uv/)? It's faster, and you don't even have to install to try it:

```bash
uv tool install token-viewer    # installs the `tokview` command
tokview start

# or run it once, no install:
uvx --from token-viewer tokview start
```

You'll see:

```
+--------------------------------------------------------------------------+
| tokview v0.0.4                                               |
|                                                                          |
|   started in background (pid 12345)                                      |
+--------------------------------------------------------------------------+

Next: tokview show --watch
Logs: /Users/you/.tokview/tokview.log
Proxy: http://127.0.0.1:4000
Browser dashboard (optional): http://127.0.0.1:3000
```

Point any app at the proxy:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export OPENAI_BASE_URL=http://127.0.0.1:4000/v1
export GOOGLE_BASE_URL=http://127.0.0.1:4000
```

Open the terminal dashboard:

```bash
tokview show --watch
```

Now make calls as usual (Anthropic SDK, OpenAI SDK, `curl`, Claude Code, whatever). They flow through the proxy. `tokview show` updates from the local SQLite database and shows spend by session, request, model, provider, and tool.

The browser dashboard is also available at <http://localhost:3000>, but it is optional. The terminal TUI gives you the core observability without npm, a browser, or a bundled frontend build.

### Track Claude Code itself

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:4000 claude
```

Every Claude Code interaction lands in `tokview show`. To inspect one session in detail:

```bash
tokview show --session <session_id>
```

## Why it's different

Three things have to be true at once, and tokview is the only tool we know of where they all are:

- **Tool-level token attribution.** Not just "this call used 180k tokens" — *which tool* spent them. It catches the dominant hidden agent cost: a big tool result (a `Read`, an MCP dump) re-billed as input on every later turn.
- **Drop-in proxy, no instrumentation.** Tracing platforms surface tool detail only if you wrap your code in their SDK. tokview gets it from one env var — for any app or CLI you can point at a URL, even ones you can't modify, like Claude Code.
- **Fully local.** SQLite on your laptop. No account, no cloud, nothing leaves your machine.

## What it shows

- $ spent today / this week / month-to-date in the terminal
- Per-provider, per-model, and per-session breakdowns
- **Session spend** — every session with calls, tokens, tool tokens, errors, cost, last activity, and model mix
- **Request breakdowns** — per-session request timelines with input/output tokens, cost, status, latency and TTFT
- **Per-tool tokens** — for agent sessions, which tools were called (`Read`, `Bash`, `mcp__…`) and how many tokens each consumed (arguments + results). Token estimates only — catches the big hidden cost: a large tool result re-sent as input on every later turn.
- **Tool hotspots** — the tools responsible for the most token volume across all sessions
- **Latency & TTFT** — time-to-first-token, total latency, and tokens/sec per model (p50/p95), plus per-call in the session detail view
- Cache-hit and reasoning-token fields are captured in SQLite for export/API use. The terminal TUI focuses on the session/request/tool views you need while an agent is running.
- Optional browser/API views can add richer charts and savings analysis over the same local database.

## What it doesn't do (intentionally)

- No team / multi-user features. Single user, localhost only.
- No virtual API keys. Your real provider keys are read from env vars and forwarded straight to the provider.
- No alerting / Slack integration. Not yet.
- No data leaves your machine. Everything in `~/.tokview/db.sqlite`.
- No prompt content stored by default. (Opt-in with redaction; see Privacy below.)

Want any of these? Open an issue. The architecture is designed to evolve into a Postgres + Docker + auth setup later — see the design spec for the "🅑 path".

## How it works

```
Your apps ──► tokview ──► Provider APIs
                       │
                       ├─ writes rows → SQLite
                       ├─ tokview show reads SQLite directly
                       └─ optional browser dashboard reads the same data
```

The proxy reads the exact token usage and cost from each provider's response object — Anthropic's `cache_creation_input_tokens` / `cache_read_input_tokens`, OpenAI's `prompt_tokens_details.cached_tokens`, Gemini's `usageMetadata`, the reasoning-tokens fields on o-series and Claude extended-thinking — and applies the right pricing tier for each. **Cost is provider-truth, not a tokenizer estimate.**

Your SDK doesn't know it's talking to a proxy. The response bytes are forwarded unchanged; the proxy tees the stream as it flies by so token capture never adds latency to your request.

**Tool-level attribution** comes from the same stream. An agent's tool calls flow through the proxy as structured blocks — `tool_use`/`tool_result` (Anthropic) or `tool_calls`/`role:tool` (OpenAI) — so tokview parses them out and tokenizes each tool's arguments and results locally. That gives you per-tool, per-session token estimates with no extra instrumentation. (It's an *estimate*, by design: the provider bills per call, not per block, and cache discounts make per-tool *cost* meaningless — so tokview reports tokens, not dollars, at the tool level. A proxy can see what a tool *returned*; it can't see the tool *execute* — that's client-side.)

## CLI

```
tokview start [-f]            start the proxy + optional browser dashboard
tokview show [-w]             terminal dashboard: sessions, requests, tools, spend
tokview show --session ID     detailed request/tool breakdown for one session
tokview stop                  graceful SIGTERM
tokview status                pid, uptime, request counts, errors, diagnostics
tokview logs [-f] [-n N]      tail the server log
tokview export --since DATE   csv/json dump of all calls since DATE
tokview reset                 wipe the SQLite database (with confirmation)
tokview version
tokview config-path
```

## Configuration

`~/.tokview/config.yaml` is auto-generated on first start. Defaults are localhost-only on ports 3000 / 4000.

```yaml
proxy:        { port: 4000, bind: 127.0.0.1 }
dashboard:    { port: 3000, bind: 127.0.0.1 }
storage:      { path: ~/.tokview/db.sqlite }
retention:    { days: 90 }
capture:      { prompts: false, responses: false }
```

Provider API keys come from environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`). tokview never reads or persists them.

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

- All dependencies on the data path (proxy engine, web framework, ASGI server) are version-pinned. Patches arrive automatically on `pipx upgrade`; major-version jumps require a tokview release.
- Runtime fetching of model-pricing data is disabled — prices come from the pinned wheel, not a network fetch.
- Default bind is `127.0.0.1`; non-loopback binds require explicit `tokview start --allow-remote` *and* the matching config setting.

Full threat model in [SECURITY.md](SECURITY.md).

## Status

`v0.0.x` — alpha. Single-user laptop tool. Works against Claude, OpenAI, Gemini, and 100+ other providers.

Roadmap lives in [CHANGELOG.md](CHANGELOG.md). Near-term:
- Cost-map refresh with hash verification
- `tokview test-providers` — smoke each configured provider with a $0.001 token
- Optional Postgres backend for multi-user use

## Contributing

PRs welcome. The loop is:

```bash
pip install -e ".[dev]"
ruff check src tests
pytest -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE). © 2026 Tejas Chopra.

Bundled open-source dependencies are credited in [NOTICES.md](NOTICES.md).

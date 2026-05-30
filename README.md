# tokview

> **Wrap your coding agent and watch where every token goes — live, in your terminal, down to the individual tool call.**

[![CI](https://github.com/chopratejas/tokview/actions/workflows/ci.yml/badge.svg)](https://github.com/chopratejas/tokview/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/token-viewer.svg)](https://pypi.org/project/token-viewer/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11--3.13-blue.svg)](https://www.python.org/downloads/)

<img src="docs/images/tokview-tui.png" alt="tokview terminal TUI: live spend, sessions, provider/model mix, cache reads, tool hotspots, and a live request tail" width="100%">

A Codex or Claude Code session burns through millions of tokens, and all you get back is a bill — or, on a subscription, nothing at all. **tokview** is a tiny local proxy that sits in front of your agent and shows you, *as it runs*, exactly where the tokens go: by session, by request, by model, and — uniquely — **by tool call**. No account, no cloud, no code changes.

## Try it in 30 seconds

```bash
uv tool install token-viewer      # PyPI name is token-viewer; the command is `tokview`
# or: pipx install token-viewer

tokview show --watch              # live terminal dashboard (one terminal)
tokview wrap claude               # run your agent through tokview (another terminal)
#   or:  tokview wrap codex
```

That's the whole workflow — **`wrap` your agent, `show` the tokens.** Agent flags pass straight through (`tokview wrap codex --model gpt-5.5 --search`), and multiple Codex/Claude sessions run at once and appear separately in `tokview show`.

## Where your tokens actually go

Most counters stop at *"this call used 180k tokens."* tokview tells you *which tool* spent them — and catches the cost nothing else surfaces: **a big tool result (a file `Read`, an MCP dump, a `grep` over your repo) gets re-sent into every later turn**, silently multiplying your input bill. Often the single largest line item in a session is one file your agent re-read a dozen times.

And it sees traffic normal token counters can't:

- **Subscription agents.** Codex and Claude Code OAuth / WebSocket traffic, not just API keys — with an **estimated equivalent API spend** per session, so you know what your subscription session *would* have cost on metered pricing.
- **Streaming, tool calls, and provider-compatible SDKs**, captured at the proxy with zero app instrumentation.

Tool-level numbers are **token estimates, not dollars** — providers bill per model call, and cache discounts make per-tool dollars misleading.

## A browser dashboard, too (optional)

When you want a wider visual scan — spend cards, cache usage, the session trace, the live tail:

<img src="docs/images/tokview-dashboard.png" alt="tokview browser dashboard: spend cards, cache reads, provider and model mix, session trace, and live tail" width="100%">

## Works with

| Client | Use | Notes |
| --- | --- | --- |
| Codex subscription | `tokview wrap codex` | HTTP + WebSocket Responses traffic, including ChatGPT-auth Codex backend calls. |
| Claude Code subscription / OAuth | `tokview wrap claude` | Native Anthropic Messages forwarding for subscription/OAuth and API-key traffic. |
| OpenAI-compatible SDKs | `OPENAI_BASE_URL=http://127.0.0.1:4000/v1` | API-key traffic through LiteLLM. |
| Anthropic-compatible SDKs | `ANTHROPIC_BASE_URL=http://127.0.0.1:4000` | Native Anthropic-compatible proxying. |
| Gemini-compatible SDKs | `GOOGLE_BASE_URL=http://127.0.0.1:4000` | Direct proxy mode. |

If a client can point at a provider-compatible base URL, tokview can usually observe it — no instrumentation required.

## What you get

- Live spend by session, request, provider, and model.
- Input, output, cache-read, cache-write, and reasoning token counters when reported.
- Estimated equivalent API spend for subscription traffic.
- Tool argument/result token estimates — including Codex shell command families like `read`, `grep`, `find`, `pytest`, and `npm`.
- A local SQLite history at `~/.tokview/db.sqlite`.

## vs. other token counters

| Approach | Good for | What tokview adds |
| --- | --- | --- |
| Provider dashboards | Billing totals | Live, local session / request / **tool** views. |
| SDK observability (Langfuse, etc.) | Instrumented apps | Wrapping a CLI you can't modify; localhost-only capture. |
| Claude/Codex log readers | Post-hoc summaries | Live proxy traffic + SDK coverage as it happens. |
| Tokenizers | Prompt-size guesses | Real provider usage, cache counters, streaming data. |

## How it works

```text
Codex / Claude / SDKs ──► tokview local proxy ──► provider backend
                                  │
                                  ├─► SQLite  ~/.tokview/db.sqlite
                                  ├─► tokview show --watch   (terminal)
                                  └─► optional browser dashboard
```

- API-key traffic routes through LiteLLM where that's the right layer.
- Codex subscription traffic uses tokview's native Codex adapter so HTTP **and** WebSocket Responses traffic are observable.
- Claude Code subscription/OAuth traffic uses tokview's native Anthropic adapter.
- Costs marked `~` are *estimated equivalent API spend* — subscription products don't bill per request like API-key calls.

## Commands

```bash
tokview wrap codex [CODEX_ARGS...]     # run Codex through tokview
tokview wrap claude [CLAUDE_ARGS...]   # run Claude Code through tokview
tokview unwrap codex                   # undo a wrap

tokview show --watch                   # live terminal dashboard
tokview show --latest                  # the most recently active session
tokview show --session SESSION_ID      # one session in detail

tokview status                         # running? counts, errors, diagnostics
tokview logs [-f] [-n N]               # tail the server log
tokview export --since YYYY-MM-DD --format csv|json
tokview reset                          # wipe the local SQLite history
tokview version
```

`tokview start` / `tokview stop` exist for debugging, but the normal workflow is `tokview wrap ...` plus `tokview show`.

## Privacy & data

By default tokview stores **accounting metadata only** — no prompt or response text:

- timestamp, latency, status
- provider, model, session id
- input / output / cache / reasoning token counters
- cost, or estimated equivalent API cost
- tool names with estimated argument/result tokens

Provider API keys come from your environment; tokview forwards them and never persists them. Everything stays in `~/.tokview/db.sqlite` on your machine.

## Configuration

`~/.tokview/config.yaml` is created on first run and defaults to localhost-only:

```yaml
proxy:      { port: 4000, bind: 127.0.0.1 }
dashboard:  { port: 3000, bind: 127.0.0.1 }
storage:    { path: ~/.tokview/db.sqlite }
retention:  { days: 90 }
capture:    { prompts: false, responses: false }
```

## Security

- Binds to `127.0.0.1` by default.
- Stores everything locally; no account, cloud service, or telemetry.
- Uses LiteLLM's installed pricing map instead of runtime pricing fetches.

Full threat model in [SECURITY.md](SECURITY.md).

## Status

`v0.0.x` — alpha. Strongest today for Codex, Claude Code, OpenAI-/Anthropic-/Gemini-compatible SDKs, and other LiteLLM-supported providers routed through the proxy.

## Contributing

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests
pytest -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE). Bundled open-source dependencies are credited in [NOTICES.md](NOTICES.md).

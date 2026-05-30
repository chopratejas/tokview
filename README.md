# tokview

> See where your LLM tokens go, by session, request, model, and tool call.

[![CI](https://github.com/chopratejas/tokview/actions/workflows/ci.yml/badge.svg)](https://github.com/chopratejas/tokview/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/token-viewer.svg)](https://pypi.org/project/token-viewer/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11--3.13-blue.svg)](https://www.python.org/downloads/)

tokview is a local proxy plus a terminal dashboard for agent spend. It is built for the workflow where you run Codex or Claude Code, let them call tools, and want to know exactly where the tokens went.

The normal UX is deliberately small:

```bash
tokview wrap codex      # launch Codex through tokview
tokview wrap claude     # launch Claude Code through tokview
tokview show --watch    # live terminal dashboard
tokview unwrap codex    # undo durable Codex routing config
```

`wrap` starts the shared local proxy if it is not already running. If it is already running, `wrap` reuses it. You can run multiple wrapped Codex sessions, multiple Claude sessions, or Codex and Claude together; they all share the same proxy and show up as separate sessions in the TUI.

The browser dashboard is optional. The terminal TUI is the primary interface and has no Node/npm dependency.


## Works With

- **Codex subscription**: `tokview wrap codex` routes Codex HTTP and WebSocket Responses traffic through tokview.
- **Claude Code subscription / OAuth**: `tokview wrap claude` launches Claude Code through tokview with native Anthropic forwarding.
- **API-key SDKs**: set `OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL`, or `GOOGLE_BASE_URL` to the tokview proxy.
- **Multiple sessions at once**: run several Codex or Claude terminals; they share one proxy and appear as separate sessions in `tokview show`.
- **No app instrumentation**: if the client can be pointed at a provider-compatible base URL, tokview can observe it.


## Screenshots

### Terminal TUI

The terminal dashboard is the primary tokview experience: sessions, tool hotspots, cache reads, estimated spend, and live request tail in one place.

<img src="docs/images/tokview-tui.png" alt="tokview terminal TUI showing tool hotspots, provider/model mix, cache reads, and live request tail" width="100%">

### Browser Dashboard

The browser dashboard is optional, but useful when you want a wider visual summary of spend, cache usage, provider mix, sessions, and recent calls.

<img src="docs/images/tokview-dashboard.png" alt="tokview browser dashboard showing spend cards, cache reads, provider and model mix, session trace, and live tail" width="100%">

## Quick Start

Install the command. The PyPI package is named `token-viewer`; the executable is `tokview`.

```bash
uv tool install token-viewer
# or
pipx install token-viewer
```

tokview supports Python 3.11 through 3.13. If needed:

```bash
pipx install --python python3.13 token-viewer
```

Start a live dashboard in one terminal:

```bash
tokview show --watch
```

Launch your agent in another terminal:

```bash
tokview wrap codex
# or
tokview wrap claude
```

Pass the agent's own flags after the tool name. tokview forwards them unchanged:

```bash
tokview wrap codex --model gpt-5.5 --search
tokview wrap claude --model opus
```

Stop routing a durable integration when you are done:

```bash
tokview unwrap codex
```

Claude wrapping is environment-based, so there is no persistent Claude config to undo today.

## What You See

The TUI updates in place and uses color when your terminal supports ANSI color. It shows:

```text
TOKVIEW
2026-05-29 22:03:44 | watch: tokview show --watch | db: ~/.tokview/db.sqlite

TODAY SPEND          7 DAY SPEND          MONTH SPEND          MONTH ERRORS
$0.5887              $0.5887              $0.5887              1
11 calls / 409k tok  11 calls / 409k tok  11 calls / 409k tok  failed provider calls

-- SESSION SPEND
SESSION                     CALLS   TOKENS  TOOL TOK  ERRORS     COST    LAST  MODELS
codex-openai-chatgpt-40af~      9  395,776    58,351       0  $0.5592  4m    gpt-5.5

-- TOOL HOTSPOTS
TOOL                 CALLS  ARGS  RESULTS   TOTAL
rtk read                23   867   56,649  57,516
rtk find                 1    33      254     287

-- LIVE TAIL
TIME      PROVIDER        MODEL        IN/OUT   CACHE      COST   ST  SESSION
22:01:37  openai-chatgpt  gpt-5.5  73,233/227  71,040  $0.0533~  200  codex-openai-chat~
```

Cost values with `~` are estimated from provider usage and LiteLLM pricing. Subscription products do not bill per request in the same way API-key calls do, so tokview shows equivalent API spend as an estimate.

## Why It Exists

Agent sessions often spend most of their tokens on tool results that get sent back into the model again and again. A normal provider bill says a request used 70k input tokens. tokview shows the breakdown:

- which session spent them
- which request spent them
- which model and provider handled them
- how many tokens were cache reads
- which tools caused the biggest token volume

For Codex shell tools, tokview groups `exec_command` by the inner command family, so you see `rtk read`, `rtk find`, `pytest`, `npm`, etc. instead of one giant `exec_command` bucket.

## Wrapping CLIs

### Codex

```bash
tokview wrap codex
```

Codex subscription mode does not reliably honor a plain `OPENAI_BASE_URL` environment variable, especially for WebSocket traffic. `tokview wrap codex` writes a small reversible block to `~/.codex/config.toml` so both HTTP and WebSocket Responses traffic route through tokview.

Undo it with:

```bash
tokview unwrap codex
```

`unwrap` restores the exact pre-wrap config when a backup exists, or removes only the tokview-managed block.

### Claude Code

```bash
tokview wrap claude
```

Claude wrapping launches Claude Code with `ANTHROPIC_BASE_URL` pointed at the local tokview proxy. Subscription/OAuth and API-key traffic are forwarded natively and normalized into the same SQLite/TUI views.

## Direct Proxy Mode

For SDKs or tools without a dedicated wrapper, point the client at the local proxy yourself. The proxy is normally started by the first `tokview wrap ...`; if you need to manage it directly, `tokview start` and `tokview stop` still exist as hidden advanced commands.

```bash
export OPENAI_BASE_URL=http://127.0.0.1:4000/v1
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
export GOOGLE_BASE_URL=http://127.0.0.1:4000
```

Then watch:

```bash
tokview show --watch
```

The optional browser dashboard is available at <http://127.0.0.1:3000>. The terminal dashboard gives the core workflow without a browser or frontend build.

## CLI

Primary commands:

```bash
tokview wrap codex [CODEX_ARGS...]
tokview wrap claude [CLAUDE_ARGS...]
tokview unwrap codex
tokview show --watch
tokview show --latest
tokview show --session SESSION_ID
```

Utilities:

```bash
tokview status
tokview logs [-f] [-n N]
tokview export --since YYYY-MM-DD --format csv|json
tokview reset
tokview config-path
tokview version
```

`tokview start` and `tokview stop` are intentionally hidden from help. They are implementation details for wrappers and debugging, not the normal UX.

## How It Works

```text
Codex / Claude / SDKs -> tokview local proxy -> provider backend
                                |
                                +-> SQLite ~/.tokview/db.sqlite
                                +-> tokview show --watch
                                +-> optional browser dashboard
```

For normal API-key traffic, LiteLLM handles provider routing and tokview records LiteLLM's standard logging payload.

For subscription CLIs, tokview uses native transport adapters:

- Codex ChatGPT subscription traffic routes to the ChatGPT Codex Responses backend and supports HTTP and WebSocket flows.
- Claude Code subscription/OAuth traffic routes through Anthropic's native Messages API.

After the response comes back, tokview normalizes usage, cache reads, reasoning tokens, estimated cost, sessions, requests, and tool rows into one local SQLite schema.

## Data Captured

By default tokview stores metadata and accounting fields only:

- timestamps and latency
- provider and model
- session id
- input/output tokens
- cache-read/cache-write token counters
- reasoning tokens when reported
- cost or estimated equivalent API cost
- tool names and estimated argument/result tokens
- status codes and error messages

No prompt text or response text is stored by default.

## Configuration

`~/.tokview/config.yaml` is created automatically. Defaults are localhost-only.

```yaml
proxy:        { port: 4000, bind: 127.0.0.1 }
dashboard:    { port: 3000, bind: 127.0.0.1 }
storage:      { path: ~/.tokview/db.sqlite }
retention:    { days: 90 }
capture:      { prompts: false, responses: false }
```

Provider API keys come from the environment (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, etc.). tokview forwards them; it does not persist them.

## Privacy

Default: token counts, cost, timing, provider/model/session metadata, and tool token estimates. No prompt text. No response text.

If full request/response capture is enabled later in config, redact patterns run before persistence.

## Security Stance

- Default bind is `127.0.0.1`.
- SQLite lives at `~/.tokview/db.sqlite`.
- Runtime fetching of model pricing is disabled; prices come from the pinned LiteLLM wheel.
- No account, no cloud service, no telemetry requirement.

See [SECURITY.md](SECURITY.md).

## Status

`v0.0.x` alpha. Single-user laptop tool. Works best today for Codex, Claude Code, OpenAI-compatible SDKs, Anthropic-compatible SDKs, Gemini-compatible SDKs, and LiteLLM-supported providers routed through the proxy.

## Contributing

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
ruff check src tests
pytest -q
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE). © 2026 Tejas Chopra.

Bundled open-source dependencies are credited in [NOTICES.md](NOTICES.md).

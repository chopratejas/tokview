# Headroom Token View — v1 Design

> **Status:** Draft for review · **Author:** tchopra · **Date:** 2026-05-27

A single-binary-feeling LLM proxy + dashboard that tracks **exact** token usage and cost across Claude, OpenAI, Gemini, and any other provider LiteLLM supports. v1 ships as `pipx install headroom-token-view && htv start` for solo laptop use; the same codebase grows into a team / SaaS deployment ("🅑") with a config change, not a rewrite.

---

## 1. Goals & Non-Goals

### v1 must
- **Drop-in proxy** for Anthropic / OpenAI / Google Gemini. The client changes one env var (`*_BASE_URL`) and gets byte-identical responses, including SSE streaming.
- **Exact cost** per request, from the provider's returned `usage` field (ground truth), priced via LiteLLM's `model_prices_and_context_window.json` — never tokenizer estimates.
- **All major modalities priced correctly**: input, output, Anthropic prompt-cache write / 5-min read / 1-hour read, OpenAI cached input, Gemini context cache, reasoning / extended thinking, image, audio.
- **Streaming-safe** usage capture: Anthropic `message_delta`, OpenAI `stream_options.include_usage` (auto-injected), Gemini `usageMetadata`.
- **One-command install** for laptop use: `pipx install headroom-token-view && htv start`. No Docker. No Postgres.
- **Branded dashboard** on `http://localhost:3000`: live cost ticker (SSE), breakdowns by provider / model / session / tag / user-agent / time range, drill-down, "missing pricing" guardrail, cache-savings panel.
- **Persistent storage** in `~/.headroom-token-view/db.sqlite` (WAL); survives restarts; CSV export.
- **"Headroom Token View" branding everywhere user-facing.** LiteLLM remains an internal dependency, never visible to end users.

### v1 explicitly does NOT include (deferred to 🅑 / v1.5+)
- Multi-user authentication
- Virtual keys (Headroom-issued keys with mapped provider keys)
- Per-user / per-team budgets and alerts
- Multi-tenancy / SaaS
- Response caching at the proxy layer
- Provider routing / fallback
- High-availability, horizontal scale, sharded storage
- Detailed prompt/response capture by default (opt-in only — see §7)

---

## 2. Architecture

### 2.1 Build path chosen
**🅐: pip-native, single Python process, SQLite, embedded SvelteKit SPA.**
Selected over:
- 🅑: docker-compose + Postgres — adds friction the solo user does not need.
- 🅒: Rust sidecar for byte forwarding — premature optimization at laptop scale (network call dominates proxy overhead).

The reasoning: at laptop QPS, LiteLLM's Python overhead is negligible (single-digit ms versus ~200ms to providers). SQLite is safe because LiteLLM runs in **stateless gateway mode** (no DB-backed virtual keys / budgets in v1), so the budget-decrement race that drives LiteLLM's "Postgres required" guidance does not apply. HTV owns all writes and emits append-only inserts; WAL handles concurrency.

### 2.2 Components (all in one Python process)

```
┌─ headroom-token-view (single process) ─────────────────────────────────────────┐
│                                                                                │
│  ┌─ LiteLLM Proxy  (port 4000) ──────────────────────────────────────────┐    │
│  │ accepts: /v1/messages, /v1/chat/completions, /v1beta/.../generateContent│    │
│  │ tees the response stream, parses provider-specific usage events,       │    │
│  │ computes cost via model_prices_and_context_window.json,                │    │
│  │ emits CustomLogger event with the full StandardLoggingPayload          │    │
│  └────────────────┬───────────────────────────────────────────────────────┘    │
│                   │ async event                                                │
│                   ▼                                                            │
│  ┌─ HTV CustomLogger ────────────────────────────────────────────────────┐    │
│  │ normalize StandardLoggingPayload → row dict                            │    │
│  │ INSERT INTO requests (batched, ≤50 rows or 100ms)                      │    │
│  │ publish to in-process pub/sub (asyncio.Queue per subscriber)           │    │
│  └────────────────┬───────────────────────────────────────────────────────┘    │
│                   ▼                                                            │
│  ┌─ FastAPI app  (port 3000) ────────────────────────────────────────────┐    │
│  │ REST   /api/summary  /api/calls  /api/sessions  /api/providers /api/  │    │
│  │ SSE    /api/events  ◄── streams spend events to dashboard              │    │
│  │ Static /            ◄── serves the embedded SvelteKit SPA              │    │
│  └────────────────┬───────────────────────────────────────────────────────┘    │
│                   ▼                                                            │
│  ┌─ Embedded SvelteKit SPA  "Headroom Token View" ───────────────────────┐    │
│  │ built once, bundled via Python package_data, served as static assets   │    │
│  │ consumes SSE for live updates + REST for backfill                      │    │
│  │ charts via Apache ECharts (svelte-echarts)                             │    │
│  └────────────────────────────────────────────────────────────────────────┘    │
│                                                                                │
│  ┌─ SQLite  (~/.headroom-token-view/db.sqlite, WAL) ─────────────────────┐    │
│  │ tables: requests, daily_rollup, kv (config)                            │    │
│  └────────────────────────────────────────────────────────────────────────┘    │
└────────────────────────────────────────────────────────────────────────────────┘
```

### 2.3 Process model

- A single Python process hosts: LiteLLM Proxy (uvicorn worker), the FastAPI dashboard backend, and serves the SPA static assets.
- LiteLLM and FastAPI run on **separate ports** (4000 and 3000) so dashboards can be exposed without exposing the proxy and vice-versa.
- A thin `multiprocessing` supervisor restarts the inner workers on crash.
- Foreground (`htv start --no-daemon`) or background (`htv start`, default).

### 2.5 Auxiliary tables (used by FastAPI for fast aggregates)
The CustomLogger writes per-request rows; the FastAPI app reads aggregates from `daily_rollup` (for the TODAY / WEEK / MTD cards) and `kv` (for last-known cost-map checksum, last successful refresh, supervisor PID, etc.). Both are SQLite tables in the same DB file; see §4 for schema.

### 2.4 Stack rationale

| Layer | Choice | Reason |
|---|---|---|
| Proxy core | **LiteLLM Proxy** (stateless / no DB) | Provider coverage + accurate cost calc + streaming-usage parsing already battle-tested. |
| Wrapper | **Python + FastAPI** | Same runtime as LiteLLM ⇒ embed as a `CustomLogger` callback, no IPC. |
| Storage | **SQLite + WAL** | Laptop-native. We own all writes; no budget-decrement race. |
| Frontend | **SvelteKit** (static build) | Small bundle, easy to embed as static, dashboard-friendly. |
| Charts | **Apache ECharts** via `svelte-echarts` | Best-in-class for time-series + categorical; lighter than Plotly. |
| Live updates | **Server-Sent Events** | Browser-native, fits cost-ticker pattern, simpler than WebSockets. |
| Distribution | **`pipx install headroom-token-view`** + `htv` CLI | One command. Brew formula later. |

---

## 3. Request Lifecycle

```
client                  HTV proxy :4000              provider                 dashboard :3000
──────                  ───────────────              ────────                 ───────────────
  │ POST /v1/messages       │                            │                         │
  │ base_url=localhost:4000 │                            │                         │
  ├───────────────────────► │                            │                         │
  │                         │ POST /v1/messages          │                         │
  │                         ├──────────────────────────► │                         │
  │                         │                            │                         │
  │ ◄── SSE chunks ─────────┤ ◄── SSE chunks ────────────┤  ▲                      │
  │   (forwarded verbatim)  │  tees stream;              │  │ parses usage from    │
  │                         │  message_delta /           │  │ provider-specific    │
  │                         │  usage chunk               │  │ events               │
  │ ◄── [DONE] ─────────────┤ ◄── [DONE] ────────────────┤                         │
  │                         │                                                      │
  │                         │ async: cost = pricing_map[model] × usage             │
  │                         │ async: INSERT INTO requests (...)                    │
  │                         │ async: pubsub.publish(spend_event) ──────────────► /api/events
  │                         │                                                      │
  │                         │                                                cost ticker
  │                         │                                                tickered live
```

### Critical properties
- **Transparent.** The proxy tees the stream — it never modifies the byte sequence going to the client.
- **Off the hot path.** DB writes and pub/sub publishes happen after `[DONE]`; the client never blocks on them.
- **Drains the entire stream.** The forwarder waits for `[DONE]` even after `finish_reason`, so vLLM-style trailing usage chunks ([LiteLLM #25389](https://github.com/BerriAI/litellm/issues/25389)) are captured.
- **Provider-specific usage parsing:**
  - **Anthropic**: `input_tokens` from `message_start`; final `output_tokens` from `message_delta` (NOT `message_stop`). `cache_creation_input_tokens` and `cache_read_input_tokens` priced separately, including the 5m vs 1h cache-read tiers.
  - **OpenAI**: `stream_options.include_usage = true` auto-injected via LiteLLM `general_settings.always_include_stream_usage`; usage arrives in the last chunk with `choices: []`. `prompt_tokens_details.cached_tokens` priced at the OpenAI cached rate.
  - **Gemini**: `usageMetadata` parsed from each chunk (typically populated in the final `finishReason: STOP` chunk). Batch / flex tier auto-detected via `usageMetadata.trafficType`.

---

## 4. Data Model

### 4.1 SQLite schema

```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;

CREATE TABLE requests (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id               TEXT UNIQUE NOT NULL,         -- LiteLLM x-request-id
    ts_ms                    INTEGER NOT NULL,             -- unix ms
    provider                 TEXT NOT NULL,                -- anthropic | openai | google | ...
    model                    TEXT NOT NULL,                -- exact model string from request
    session_id               TEXT,                         -- x-litellm-session-id header
    user                     TEXT,                         -- request-body "user" field
    tags                     TEXT,                         -- JSON array
    user_agent               TEXT,                         -- claude-cli/... openai-python/...
    team_id                  TEXT,                         -- v1.5+ ; nullable; reserved
    -- ground-truth tokens (provider 'usage' object)
    input_tokens             INTEGER NOT NULL,
    output_tokens            INTEGER NOT NULL,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,   -- Anthropic write
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,   -- Anthropic / OpenAI / Gemini
    cache_read_1h_tokens     INTEGER NOT NULL DEFAULT 0,   -- Anthropic 1h tier
    reasoning_tokens         INTEGER NOT NULL DEFAULT 0,   -- o1/o3/extended thinking
    image_tokens             INTEGER NOT NULL DEFAULT 0,
    audio_tokens             INTEGER NOT NULL DEFAULT 0,
    -- cost
    cost_usd                 REAL NOT NULL,
    cost_estimated           INTEGER NOT NULL DEFAULT 0,   -- 1 = tokenizer-estimated (disconnect)
    -- stream + status
    is_stream                INTEGER NOT NULL,
    completed                INTEGER NOT NULL,             -- 0 = client disconnected mid-stream
    latency_ms               INTEGER,
    status_code              INTEGER,
    error_message            TEXT,
    -- optional captured content (see §7)
    prompt_text              TEXT,                         -- NULL unless capture.prompts
    response_text            TEXT                          -- NULL unless capture.responses
);

CREATE INDEX idx_req_ts          ON requests(ts_ms);
CREATE INDEX idx_req_prov_model  ON requests(provider, model);
CREATE INDEX idx_req_session     ON requests(session_id);
CREATE INDEX idx_req_user        ON requests(user);

CREATE TABLE daily_rollup (
    day                      TEXT PRIMARY KEY,             -- YYYY-MM-DD UTC
    requests_count           INTEGER NOT NULL,
    cost_usd                 REAL NOT NULL,
    input_tokens             INTEGER NOT NULL,
    output_tokens            INTEGER NOT NULL,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    by_provider              TEXT NOT NULL,                -- JSON
    by_model                 TEXT NOT NULL                 -- JSON
);

CREATE TABLE kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL,
    updated_ms INTEGER NOT NULL
);
```

### 4.2 Maintenance

**Rollup update strategy — application-layer, not a trigger.** The CustomLogger flusher (which batches up to 50 rows or 100 ms of inserts) computes a per-day delta in memory across the batch and issues **one UPSERT** to `daily_rollup` per affected day per flush. Rationale: a per-row `AFTER INSERT` trigger would fire 50× per flush against the same hot row, multiplying contention without value. App-layer rollup keeps writes amortized and makes the rollup logic explicit (no hidden trigger semantics). A nightly **reconciliation pass** recomputes `daily_rollup` from scratch over the prior 7 days to heal any drift.

- Retention sweeper deletes `requests` rows older than `retention.days` (configurable; default 90) on a daily timer; rollups outlive the raw rows.
- Schema is **Postgres-portable**: same DDL works under Postgres with trivial type swaps (`AUTOINCREMENT` → `BIGSERIAL`, `INTEGER` → `BIGINT` where appropriate). Migration to 🅑 = swap driver + run schema script + replay reconciliation.

---

## 5. Dashboard (v1)

### 5.1 Layout

```
╔════════════════════════════════════════════════════════════════════════════════════╗
║  Headroom Token View                                                  ⚙  ●LIVE    ║
╠════════════════════════════════════════════════════════════════════════════════════╣
║                                                                                    ║
║   TODAY                  THIS WEEK              MTD              CACHE SAVED       ║
║   $42.18  ↑42 calls      $187.40  ↑512 calls    $812.55          $124.30  (73%)    ║
║                                                                                    ║
║   ┌─ Cost / minute (last hour) ──────────────────────────────────────────────┐    ║
║   │     ▁▂▃▅▇█▇▅▃▂▁▁▁▂▃▅▇█▇▅▃▂▁▁▁▂▃▅▇█▇▅▃▂▁▁▁▂▃▅▇█▇▅▃                       │    ║
║   └──────────────────────────────────────────────────────────────────────────┘    ║
║                                                                                    ║
║   ┌─ By provider ─────────┐  ┌─ By model ──────────┐  ┌─ By session ──────────┐   ║
║   │ Claude   $28.40 ████▍ │  │ claude-3-7-sonnet ▌│  │ claude-code-…  $35.42 ▌│   ║
║   │ OpenAI   $11.05 ██▌   │  │ gpt-4o            ▎│  │ session-a3f1    $4.20 ▎│   ║
║   │ Gemini    $2.73 █▎    │  │ gemini-2.5-pro    ▏│  │ session-bb09    $2.18 ▏│   ║
║   └───────────────────────┘  └────────────────────┘  └────────────────────────┘   ║
║                                                                                    ║
║   ┌─ Live tail (last 20) ──────────────────────────────────────────────────────┐  ║
║   │  ts        provider  model               in→out      cost    session         │  ║
║   │  15:42:11  anthropic claude-3-7-sonnet  1.2k → 412   $0.018  session-a3f1   │  ║
║   │  15:42:09  openai    gpt-4o             4.1k → 380   $0.012  session-bb09   │  ║
║   │  15:42:04  google    gemini-2.5-pro     890  → 220   $0.003  claude-code-…  │  ║
║   └────────────────────────────────────────────────────────────────────────────┘  ║
╚════════════════════════════════════════════════════════════════════════════════════╝
```

### 5.2 Filters
Top bar: provider · model · session · tag · user-agent · time range (last 1h / 24h / 7d / 30d / custom). All filters apply to every panel.

### 5.3 Pages
- **Overview** (above) — default landing.
- **Sessions** — list of sessions with cost, model mix, duration.
- **Calls** — paginated requests table with full filtering; click for detail drawer.
- **Errors** — last N 4xx/5xx (provider + client-side wrong-baseurl 404s).
- **Health** — proxy uptime, last cost-map refresh, missing-pricing alerts.

### 5.4 Real-time
The dashboard uses a **two-call pattern** on connect:

1. `GET /api/summary` (and `/api/calls?since=…`) — fetches current aggregates + last N events to paint initial state.
2. `GET /api/events` (SSE) — subscribes to live spend events; each event arrives as `data: {<spend payload JSON>}\n\n`.

On reconnect after a network blip, the SPA re-issues `/api/calls?since=<last_seen_ts_ms>` to backfill the gap, then re-subscribes to `/api/events`. The SSE endpoint itself does not retain history — backfill is the dedicated job of the REST endpoints. This keeps the SSE channel simple (no replay buffer, no resume tokens) and shifts complexity to REST, which is already paginated.

---

## 6. Onboarding & Configuration

### 6.1 Install + start

```
$ pipx install headroom-token-view
$ htv start
```

First-run banner:

```
╔══════════════════════════════════════════════════════════════════════════╗
║  Headroom Token View v0.1.0                                              ║
║  ✓ Proxy:     http://localhost:4000                                      ║
║  ✓ Dashboard: http://localhost:3000                                      ║
║  ✓ DB:        ~/.headroom-token-view/db.sqlite                           ║
║                                                                          ║
║  Point your apps at the proxy:                                           ║
║    Anthropic:    export ANTHROPIC_BASE_URL=http://localhost:4000         ║
║    OpenAI:       export OPENAI_BASE_URL=http://localhost:4000/v1         ║
║    Gemini:       export GOOGLE_BASE_URL=http://localhost:4000            ║
║    Claude Code:  ANTHROPIC_BASE_URL=http://localhost:4000 claude         ║
║                                                                          ║
║  Open the dashboard: http://localhost:3000                               ║
╚══════════════════════════════════════════════════════════════════════════╝
```

### 6.2 Config (`~/.headroom-token-view/config.yaml`)

```yaml
proxy:        { port: 4000, bind: 127.0.0.1 }
dashboard:    { port: 3000, bind: 127.0.0.1 }
storage:      { path: ~/.headroom-token-view/db.sqlite }
litellm:
  always_include_stream_usage: true     # critical for OpenAI streams
pricing:
  refresh_interval_hours: 24
  alert_on_zero_cost: true              # warn when tokens > 0 and cost = $0
  fallback_to_last_known_good: true     # rollback on malformed cost map
retention:    { days: 90 }
capture:      { prompts: false, responses: false }
```

### 6.3 CLI

| Command | Behavior |
|---|---|
| `htv start [--no-daemon]` | Start both services; daemonize by default. |
| `htv stop` | Graceful shutdown. |
| `htv status` | Up? since when? requests today? cost today? last error? |
| `htv logs [--tail]` | Tail the combined log. |
| `htv export --since YYYY-MM-DD [--format csv|json]` | Export requests. |
| `htv reset --yes` | Wipe DB (interactive confirm required). |

### 6.4 Trust boundary

- Defaults bind to **`127.0.0.1`** only.
- Provider API keys are read from environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`) by LiteLLM. Never persisted by HTV.
- v1 has no auth on the dashboard — localhost-only.
- The `proxy.bind` / `dashboard.bind` config keys *do* accept non-loopback addresses, but doing so requires the explicit CLI flag `htv start --allow-remote`. Without it, HTV refuses to start with a non-loopback bind and prints a warning explaining that v1 has no authentication and team-deploys belong on the 🅑 path (auth + Postgres). This is an operator-acknowledged escape hatch, not a recommended mode.

---

## 7. Privacy & Content Capture

**Default: HTV stores only token counts + cost + metadata. No prompt or response text.**

Rationale:
- Anthropic prompts can run to hundreds of thousands of tokens — SQLite size balloons fast.
- Prompts often carry PII, secrets, and proprietary IP — storing them must be a deliberate decision.
- v1 ask was "track tokens," not "log everything."

**Opt-in capture mode:**

```yaml
capture:
  prompts: true
  responses: true
  redact_patterns:
    - '(sk|pk)-[A-Za-z0-9]{20,}'           # API keys
    - '[\w.+-]+@[\w-]+\.[\w.-]+'           # emails
  max_chars_per_field: 8192                # truncate, do not refuse
```

**Redaction is applied *before* persistence.** The raw text is matched against every `redact_patterns` regex inside the CustomLogger, matches are replaced with `[REDACTED:<rulename>]`, and only the post-redaction string is written to SQLite. The DB never holds the unredacted secret. The request-detail drawer renders the stored (already-redacted) text with a 🛡️ badge per replacement. Captured fields live in `requests.prompt_text` / `requests.response_text` (default NULL).

---

## 8. Error Handling & Edge Cases

| Scenario | Behavior |
|---|---|
| Provider 5xx / timeout | Forward upstream status & body verbatim. Row logged with `cost_usd=0`, `status_code`, `error_message`. Surfaced in **Errors** tab. |
| **Client disconnect mid-stream** ([LiteLLM #14457](https://github.com/BerriAI/litellm/issues/14457)) | HTV detects the unclosed SSE stream. On disconnect: runs the provider's local tokenizer (Anthropic via `anthropic-tokenizer`, OpenAI via `tiktoken`) over `(prompt + chunks-received-so-far)` to estimate output tokens. Gemini ships no local tokenizer — its `count_tokens` is a remote endpoint, so HTV calls it with a **200 ms timeout** and on timeout/failure falls back to a `len(text)/4` char-based estimate. Either way, the row is stored with `completed=0`, `cost_estimated=1`, and the dashboard shows a ⚠ on estimated rows. The estimate is a best-effort placeholder; the source-of-truth remains the provider's eventual billed usage if a webhook or batch reconciliation ever exposes it. |
| Unknown model / missing pricing ([incident 2026-01-27](https://docs.litellm.ai/blog/model-cost-map-incident)) | Guardrail: `total_tokens > 0 AND cost_usd = 0` → row flagged + dashboard alert *"Missing pricing for model X — update HTV or override in config."* |
| Pricing-map staleness | Daily fetch of `model_prices_and_context_window.json` with SHA-256 verify; on malformed JSON, retain the last-known-good copy on disk. |
| SQLite locked under burst | WAL + `busy_timeout=5000` + writes batched (≤50 rows or 100 ms flush). |
| Wrong `base_url` in client → 404 | Dashboard **Health** tab shows last 10 4xx/5xx from clients for quick diagnosis. |
| Disk fills | Retention sweeper deletes rows > `retention.days` daily. |
| Process crash | `htv start` runs under a `multiprocessing` supervisor; restarts the inner workers on crash. |
| Pricing override needed | `config.yaml` exposes `pricing.overrides` map (model → input/output/cache rates) layered on top of the cost map. |

---

## 9. Testing

| Layer | Strategy |
|---|---|
| **Pricing math** | Property tests over random `usage` payloads + pricing rows. Snapshot tests with hand-computed expected costs per major model (claude-3-7-sonnet, gpt-4o, gemini-2.5-pro). Cover cache tiers, reasoning tokens, image/audio. |
| **Streaming parser** | Recorded SSE fixtures from real provider responses (replayed via `pytest-httpx`); assert captured token counts == known-good. Edge cases: vLLM trailing usage ([#25389](https://github.com/BerriAI/litellm/issues/25389)), Anthropic `message_delta`, OpenAI `stream_options.include_usage`. |
| **Disconnect handling** | Integration: client cancels mid-stream → row written with `completed=0` and tokenizer estimate within ±10% of provider's actual usage. |
| **Dashboard SSE** | Playwright e2e: trigger a call → cost ticker increments within 1 s. |
| **End-to-end smoke** | `HTV_E2E=1` gated test that hits each real provider once. Skipped in CI by default to preserve quota. |
| **Pricing-map regression** | CI fetches latest cost map; warns on `>5%` price delta per model day-over-day (catches incidents like 2026-01-27). |

---

## 10. Graduation Path to 🅑 (Team / SaaS)

Designed so the v1 → 🅑 jump is **configuration, not a rewrite.**

| Concern | v1 (laptop) | 🅑 (team) | What changes |
|---|---|---|---|
| Storage | SQLite | Postgres | Set `DATABASE_URL`; same DDL with type swaps. |
| Process | One Python process | docker-compose | Same code split across containers. |
| Auth | None (localhost only) | Magic-link / SSO | Add FastAPI auth middleware. |
| Provider keys | Passthrough from env | Virtual keys (`htv-sk-…`) | Enable LiteLLM virtual-keys feature (DB-backed). |
| Tenancy | Single user | Multi-user / team | `team_id` column already present (nullable); add filters. |
| Retention | 90 days | Configurable / unlimited | Already a config knob. |

Forward-compat: `requests.team_id` is nullable in v1; `daily_rollup` intentionally does NOT carry `team_id` in v1 (single-tenant). On migration to 🅑 we **regenerate rollups from `requests`** with `team_id` as a grouping key — the nightly reconciliation pass (§4.2) is the same code; we just run it once after migration. Config has a `tenant_id` namespace placeholder; CSS supports dark/light + brand-token swap.

---

## 11. Open Questions

_None at sign-off. (To be filled if review surfaces any.)_

---

## 12. References

### Provider streaming behavior
- Anthropic streaming (`message_delta` for final usage): https://platform.claude.com/docs/en/build-with-claude/streaming
- OpenAI `stream_options.include_usage`: https://community.openai.com/t/usage-stats-now-available-when-using-streaming-with-the-chat-completions-api-or-completions-api/738156
- Gemini `usageMetadata`: https://ai.google.dev/gemini-api/docs/openai

### LiteLLM (the engine)
- Spend tracking: https://docs.litellm.ai/docs/proxy/cost_tracking
- Prompt caching pricing: https://docs.litellm.ai/docs/completion/prompt_caching
- Custom callbacks: https://docs.litellm.ai/docs/observability/custom_callback
- Sessions: https://docs.litellm.ai/docs/proxy/ui_logs_sessions
- Request tags: https://docs.litellm.ai/docs/proxy/request_tags
- Cost-discrepancy debugging: https://docs.litellm.ai/docs/troubleshoot/cost_discrepancy
- Cost-map incident (2026-01-27): https://docs.litellm.ai/blog/model-cost-map-incident
- `model_prices_and_context_window.json`: https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json

### Known LiteLLM issues factored into design
- Mid-stream disconnect → $0 logged: https://github.com/BerriAI/litellm/issues/14457
- vLLM trailing-usage chunk dropped: https://github.com/BerriAI/litellm/issues/25389
- OpenRouter `usage.cost` lost on streams: https://github.com/BerriAI/litellm/issues/16021
- `x-litellm-response-cost` header missing on streams: https://github.com/BerriAI/litellm/issues/12689
- Anthropic cache double-count (fixed): https://github.com/BerriAI/litellm/issues/9812

### Comparable systems considered & rejected
- TensorZero (Rust + ClickHouse): https://www.tensorzero.com/docs/gateway — ClickHouse breaks the laptop constraint.
- Helicone — acquired by Mintlify Mar 2026; roadmap uncertain.
- Langfuse — SDK-level wrapper, not a forward proxy.
- Glide (EinStack) — lean Go gateway, no UI.
- Portkey, Vercel AI Gateway — hosted only.

# Changelog

All notable changes to tokview will be recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.0.2] — 2026-05-29

### Added
- **Per-tool token tracking.** Parses `tool_use`/`tool_result` blocks out of
  agent conversations (both Anthropic and OpenAI message shapes) and records,
  per session, which tools were called and how many tokens each consumed —
  arguments + results. Surfaced in the session view as a "Tools used" table.
  - **Token estimates only, no cost.** The provider bills per call, not per
    block, and cache discounts make per-tool cost meaningless; token counts are
    honest, so that's all we report.
  - Deduped by `tool_call_id` (the full conversation is re-sent each turn) via
    a `tool_calls` table + an in-process seen-id cache to avoid re-tokenizing.
  - This catches the dominant hidden agent cost: large tool results (a big
    `Read`, an MCP search dump) that get re-sent as input on every later turn.

### Notes
- A proxy can see tool *intent and results* (they flow through the prompt) but
  not tool *execution* (the actual file read / MCP transport is client-side).

## [0.0.1] — 2026-05-28

First release. A small, local, single-user token viewer.

### Added
- Drop-in proxy on `:4000` + SQLite-backed dashboard on `:3000`, in a single
  daemonized Python process. Point any app at the proxy with one env var.
- Exact per-call cost from the provider's own `usage` object (Anthropic
  prompt-cache write / 5-min read / 1-hour read, OpenAI cached tokens, Gemini
  context cache, Claude extended-thinking / o-series reasoning tokens) — never
  a tokenizer estimate.
- **Latency & TTFT**: captures time-to-first-token and total latency per call;
  per-model p50/p95 latency, TTFT, and tokens/sec at `/api/latency` and in the
  live tail.
- **Savings coach** (`/api/insights`): deterministic, local, no model calls.
  Flags repeated uncached prompts that could be cached, reports caching savings
  already realized, and offers a per-session cheaper-model what-if — all priced
  from the local cost map.
- **Session waterfall** (`/api/sessions/{id}`): a timeline view of every call in
  an agent session with cost, tokens, latency, and TTFT.
- Aggregate REST endpoints: `/api/summary` `/api/calls` `/api/providers`
  `/api/models` `/api/sessions` `/api/diagnostics`.
- Live cost ticker via SSE `/api/events` with an in-process pub/sub fan-out.
- SvelteKit 5 + ECharts dashboard at `/`.
- CLI: `tokview start [-f] [--allow-remote] | stop | status | logs | export |
  reset | version | config-path`. Daemonized by default.
- Tokenizer-estimated input tokens (`cost_estimated=1`) for failure /
  mid-stream-disconnect rows.
- 64-test pytest suite (db, dashboard, logger, pubsub, insights).
- GitHub Actions CI (lint + test + wheel build) and Trusted-Publishing
  workflow for PyPI.

### Security
- `LITELLM_LOCAL_MODEL_COST_MAP=True` set before LiteLLM import; cost map comes
  from the installed wheel, not a runtime GitHub fetch (the vector for the
  2026-01-27 LiteLLM cost-map incident).
- LiteLLM soft-pinned `>=1.86.1,<2.0.0`; a major upgrade requires a tokview
  release.
- Default bind `127.0.0.1`. A non-loopback bind requires `--allow-remote` on
  the CLI **and** the corresponding config setting.

[Unreleased]: https://github.com/chopratejas/tokview/compare/v0.0.2...HEAD
[0.0.2]: https://github.com/chopratejas/tokview/releases/tag/v0.0.2
[0.0.1]: https://github.com/chopratejas/tokview/releases/tag/v0.0.1

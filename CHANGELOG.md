# Changelog

All notable changes to Headroom Token View will be recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.0.1] — 2026-05-27

First release. Solo-laptop, single-tenant v1.

### Added
- LiteLLM-backed proxy on `:4000` with SQLite-backed spend logging on `:3000`.
- `HtvLogger` CustomLogger captures the provider's `usage` object (Anthropic
  prompt-cache write / 5-min read / 1-hour read, OpenAI cached tokens, Gemini
  context cache, Claude extended-thinking / o-series reasoning tokens).
- Aggregate REST endpoints: `/api/summary` `/api/calls` `/api/providers`
  `/api/models` `/api/sessions` `/api/diagnostics`.
- Live cost ticker via SSE `/api/events` with an in-process pub/sub fan-out.
- Branded SvelteKit 5 + ECharts dashboard at `/`.
- CLI: `htv start [-f] [--allow-remote] | stop | status | logs | export | reset
  | version | config-path`. Daemonized by default.
- Tokenizer-estimated input tokens (`cost_estimated=1`) for failure /
  mid-stream-disconnect rows.
- 37-test pytest suite covering db, dashboard, logger, pubsub.
- GitHub Actions CI (lint + test + wheel build) and Trusted-Publishing
  workflow for PyPI.

### Security
- `LITELLM_LOCAL_MODEL_COST_MAP=True` set before LiteLLM import; cost map
  comes from the pinned wheel, not a runtime GitHub fetch (vector for the
  2026-01-27 LiteLLM cost-map incident).
- LiteLLM soft-pinned `>=1.86.1,<2.0.0`. Major upgrades require an HTV
  release.
- Default bind `127.0.0.1`. Non-loopback bind requires `--allow-remote`
  on the CLI **and** the corresponding config setting.

[Unreleased]: https://github.com/chopratejas/headroom-token-view/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/chopratejas/headroom-token-view/releases/tag/v0.0.1

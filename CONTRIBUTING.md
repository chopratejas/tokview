# Contributing to tokview

Thank you for thinking about contributing. This guide explains how to get the project running locally, the loop we run before merging, and how to navigate the codebase.

## Quickstart for contributors

```bash
git clone https://github.com/chopratejas/tokview.git
cd tokview

# Python deps + editable install
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# JS deps for the dashboard
(cd web && npm install && npm run build)

# Verify everything works
.venv/bin/ruff check src tests
.venv/bin/pytest -q
```

You should see `All checks passed!` and `37 passed`.

## The contribution loop

For any change, before you open a PR:

1. **Lint** — `ruff check src tests`.
2. **Test** — `pytest -q`. If you're touching cost / token / streaming logic, add a `tests/test_logger.py` case for it.
3. **Smoke** — `.venv/bin/tokview start`, send a real call through, refresh the dashboard, `tokview stop`.
4. **Tight commit messages** — see existing commits for the house style: present tense, short subject, explain the *why* in the body, end with `Co-Authored-By:` if AI-assisted.

## Project layout

```
tokview/
├── docs/superpowers/specs/      # design spec (the source of truth for v1)
├── src/tokview/
│   ├── cli.py                   # `tokview ...` commands
│   ├── server.py                # orchestrator: LiteLLM + FastAPI in one process
│   ├── dashboard.py             # FastAPI app + endpoints + SPA mount
│   ├── db.py                    # SQLite layer (aiosqlite)
│   ├── logger.py                # TokviewLogger CustomLogger — the heart of cost capture
│   ├── pubsub.py                # In-process async fan-out for SSE
│   ├── config.py                # Pydantic config + ~/.tokview/config.yaml
│   └── litellm_config.py        # Generates the LiteLLM proxy config
├── tests/                       # pytest suite (37 cases)
├── web/                         # SvelteKit 5 + ECharts SPA
├── .github/workflows/           # CI + Trusted-Publishing
├── pyproject.toml               # Package metadata, deps, ruff, pytest
└── README.md
```

## Where to put new code

| Want to... | Edit... |
|---|---|
| Add a new dashboard endpoint | `src/tokview/dashboard.py` |
| Add a new SQL aggregate | `src/tokview/db.py` |
| Change how a provider's usage is mapped to a row | `src/tokview/logger.py::TokviewLogger._build_row` + a test in `tests/test_logger.py` |
| Add a CLI command | `src/tokview/cli.py` |
| Add a config knob | `src/tokview/config.py` (Pydantic model) |
| Change the dashboard UI | `web/src/routes/+page.svelte` (+ rebuild via `npm run build`) |

## Coding conventions

- **Python**: `from __future__ import annotations` at the top of every module; type-annotate public functions; keep modules focused (one responsibility, ideally < ~400 lines).
- **Ruff**: configured in `pyproject.toml`; rules `E F I B UP SIM RUF`. A small ignore list explains style preferences.
- **Tests**: synthetic payloads, not network. If a test needs a real provider, gate it on `TOKVIEW_E2E=1` so CI doesn't burn quota.
- **Defensive field extraction**: LiteLLM reshuffles `StandardLoggingPayload` between releases. Use `dict.get(...)` chains with safe defaults; never assume a path exists.
- **Comments**: a one-liner when the *why* is non-obvious. The *what* is the code itself.
- **Commits**: present tense subject ≤ 70 chars; body explains motivation, not the diff.

## Cost / token math changes — the careful path

This is the most load-bearing part of tokview. If you're changing how usage flows from LiteLLM into our rows:

1. Look at the LiteLLM release notes since `litellm[proxy]==1.86.1` to see if anything moved.
2. Write the test first in `tests/test_logger.py` against a synthetic StandardLoggingPayload + response object.
3. Implement; ensure the existing 14 logger cases still pass.
4. Add a `Co-Authored-By` if AI helped — it's normal here; the spec was AI-assisted too.

## Reporting bugs / requesting features

Use the GitHub issue templates. For bugs in cost calculation specifically, include the **provider**, **model**, **stream vs non-stream**, the `usage` object from the response (redact prompts), and the cost tokview recorded.

## Security disclosure

See [SECURITY.md](SECURITY.md).

## License

By contributing you agree your contributions are licensed under the project's [MIT license](LICENSE).

# Headroom Token View

A drop-in LLM proxy + dashboard that tracks **exact** token usage and cost across Claude, OpenAI, Gemini, and any provider [LiteLLM](https://github.com/BerriAI/litellm) supports.

```
$ pipx install headroom-token-view
$ htv start

  ✓ Proxy     http://localhost:4000
  ✓ Dashboard http://localhost:3000

  Point your apps at the proxy:
    export ANTHROPIC_BASE_URL=http://localhost:4000
    export OPENAI_BASE_URL=http://localhost:4000/v1
    export GOOGLE_BASE_URL=http://localhost:4000
```

That's it. No Docker. No Postgres. SQLite + a single Python process.

## Status

Pre-alpha. v1 in active development.

See [`docs/superpowers/specs/2026-05-27-headroom-token-view-design.md`](docs/superpowers/specs/2026-05-27-headroom-token-view-design.md) for the design.

## Security & dependency policy

HTV sits between every app you own and every provider you bill against, so it sees prompts and API keys. Two stances:

1. **Soft-pinned LiteLLM** (`>=1.86.1,<2.0.0`). Patches and minors of LiteLLM 1.x land automatically on `pipx upgrade`. New models are picked up without waiting for an HTV release. A future LiteLLM 2.0 (which is allowed to break things by the project's own versioning policy) is held back until HTV verifies it.

2. **Cost map fetched from the bundled wheel, not GitHub.** HTV sets `LITELLM_LOCAL_MODEL_COST_MAP=True` before importing LiteLLM, which disables the default behavior of fetching `model_prices_and_context_window.json` from `main` at runtime. That auto-fetch was the vector for the [2026-01-27 cost-map incident](https://docs.litellm.ai/blog/model-cost-map-incident) (malformed JSON → $0 logged for new models). A future iteration will add HTV's own refresh path with explicit SHA-256 verification.

If you need stricter determinism (regulated environment), pin HTV exactly (`pipx install headroom-token-view==0.0.1`) and accept the trade-off of slower new-model coverage.

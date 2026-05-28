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

# Security Policy

Headroom Token View sits between every app you own and every provider you bill against. It sees prompts and API keys. We take that seriously.

## Supported versions

Until 1.0, only the latest minor on the `main` branch receives fixes.

## Reporting a vulnerability

**Do not** open a public GitHub issue for security problems.

Email **chopratejas@gmail.com** with the subject line `[HTV SECURITY] ...`. Include:

- A description of the vulnerability and the affected version
- Steps to reproduce
- Any logs, traffic captures, or exploit code (avoid sharing real customer data)
- Your preferred contact for follow-up

We'll acknowledge within 72 hours and target a patch within 14 days for high-severity findings. Coordinated disclosure preferred — we'll credit you in the release notes unless you'd rather stay anonymous.

## Threat model

HTV's stance, by design:

| Threat | Mitigation |
|---|---|
| **LiteLLM package compromised on PyPI** (data-path dependency) | Soft-pinned to `litellm[proxy]>=1.86.1,<2.0.0`. Major-version jumps must come through an HTV release that's been re-tested. Future hardening: hash-verified lockfile (`requirements.lock`) for `pip install --require-hashes`. |
| **LiteLLM auto-fetches `model_prices_and_context_window.json` from GitHub `main` at runtime** | **Disabled by default.** HTV sets `LITELLM_LOCAL_MODEL_COST_MAP=True` before importing LiteLLM, so prices come from the wheel that's installed, not a runtime fetch. The 2026-01-27 [cost-map incident](https://docs.litellm.ai/blog/model-cost-map-incident) — malformed JSON in main caused new Azure GPT-5.2 calls to be logged at $0 — would have been invisible to HTV under this setting. |
| **Transitive deps compromised** (~100 packages from LiteLLM) | Soft-pinned per-major. CI runs `ruff` + the full test suite on every push. Future: scheduled `pip-audit` job. |
| **Mid-stream client disconnect leaves spend uncounted** ([LiteLLM #14457](https://github.com/BerriAI/litellm/issues/14457)) | HtvLogger runs `litellm.token_counter()` over the request when no provider-reported usage is available, marks the row `cost_estimated=1` so the dashboard can badge it. Char-based fallback for Gemini (no local tokenizer). |
| **Unknown / new model has no pricing → silent $0** | Guardrail: rows where `total_tokens > 0 AND cost = 0 AND cost_estimated = 0` are counted in `/api/diagnostics.metrics.missing_pricing` and surfaced via `htv status`. |
| **Provider API keys leaked to logs** | HTV never reads or persists provider keys. Keys live in environment variables and are forwarded by LiteLLM directly to the provider. The HTV log file (`~/.headroom-token-view/htv.log`) contains LiteLLM proxy logs + uvicorn access logs only. |
| **Dashboard exposed to the network** | Default `bind: 127.0.0.1`. A non-loopback bind requires *both* the config setting AND the explicit `htv start --allow-remote` flag. v1 has no authentication; we refuse to start without explicit operator opt-in. |
| **Prompt content stored at rest** | Default off. When opt-in capture is enabled, regex-based redaction runs *before* persistence — the database never holds the raw secret. |

## What HTV does NOT defend against

- **A compromised local machine.** If an attacker has shell on your laptop, they can read `~/.headroom-token-view/db.sqlite` (and your shell env, which has your API keys). HTV doesn't encrypt SQLite at rest.
- **A malicious LiteLLM RCE released via PyPI.** Soft pinning means new minors arrive on `pipx upgrade`. If you need stricter determinism, pin HTV exactly (`pipx install headroom-token-view==0.0.1`).
- **MITM on `pip install`.** Use a trusted PyPI mirror and verify the downloaded wheel's hash matches PyPI's published hash.

## Encryption at rest (planned)

The v1.0 graduation path (Postgres + Docker) will add `pgcrypto`-based field encryption for opt-in captured prompts. SQLite encryption (via SEE / SQLCipher) is on the roadmap for the laptop tier — issue [TBD].

## Defensive recommendations for operators

- Keep `htv` updated (`pipx upgrade headroom-token-view`).
- Don't `--allow-remote` unless you've put authentication in front of HTV (reverse proxy + OAuth, or wait for v1.0's built-in auth).
- Use the OS firewall to block external access to `:3000` and `:4000` on a shared machine.
- Periodically review `htv status` for `missing_pricing` and `errors_24h` counters.
- For production / regulated environments, pin HTV exactly and run the test suite against your locked LiteLLM version before upgrade.

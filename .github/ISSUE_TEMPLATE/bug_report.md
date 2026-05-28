---
name: Bug report
about: Something broken in Headroom Token View
title: "[bug] "
labels: bug
---

## Summary

<!-- One sentence: what's broken? -->

## Reproduction

```bash
# The exact commands you ran
```

## Expected vs. actual

- **Expected:** ...
- **Actual:** ...

## Environment

- HTV version: `htv version`
- Python: `python --version`
- OS:
- LiteLLM version (auto-resolved): `.venv/bin/pip show litellm | head -2`

## Logs

<details>
<summary>htv logs -n 100 (paste below)</summary>

```
```
</details>

## For cost / token bugs specifically

If the dashboard shows a wrong cost or token count:

- **Provider:** (anthropic / openai / google / other)
- **Model:** (exact string sent in the request)
- **Stream:** yes / no
- **Response `usage` object** (redact prompts):

```json
{
  "prompt_tokens": ...,
  "completion_tokens": ...,
  "...": "..."
}
```

- **HTV recorded:** `cost_usd=X.XXXXX`, `input_tokens=N`, `output_tokens=M`, ...
- **Expected (per provider docs / billing):**

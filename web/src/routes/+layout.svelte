<script>
  let { children } = $props();
</script>

{@render children?.()}

<style>
  :global(:root) {
    --bg: #0e1117;
    --panel: #161b22;
    --panel-2: #1c2330;
    --border: #2a313c;
    --text: #e6edf3;
    --text-dim: #8b949e;
    --accent: #7c5cff;
    --accent-2: #58a6ff;
    --good: #3fb950;
    --warn: #d29922;
    --bad: #f85149;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  }
  :global(*) { box-sizing: border-box; }
  :global(body) {
    margin: 0;
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    color: var(--text);
    background: var(--bg);
  }
  :global(code) {
    font-family: var(--mono);
    font-size: 12px;
    background: var(--panel-2);
    padding: 2px 6px;
    border-radius: 4px;
  }
  :global(a) { color: var(--accent-2); text-decoration: none; }
</style>

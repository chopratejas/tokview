<script>
  import { onMount, onDestroy } from 'svelte';
  import { fetchSummary, fetchCalls, fetchProviders, fetchModels, fetchSessions, subscribe } from '$lib/api.js';
  import { fmtUsd, fmtUsdSmall, fmtNum, fmtTs, truncate } from '$lib/format.js';

  // ---------- state ----------
  let summary = $state(null);
  let calls = $state([]);
  let providers = $state([]);
  let models = $state([]);
  let sessions = $state([]);
  let connected = $state(false);
  let chartEl;
  let chart;          // ECharts instance
  let pollInterval;
  let eventSource;
  let highlightIds = $state(new Set());

  // ---------- effects ----------
  $effect(() => {
    // Render the minute-series chart whenever summary updates
    if (chart && summary?.minute_series) {
      const buckets = 60;
      const bucketMs = 60_000;
      const asOf = summary.as_of_ms ?? Date.now();
      const startMs = (Math.floor(asOf / bucketMs) - buckets + 1) * bucketMs;
      const data = new Array(buckets).fill(0);
      for (const row of summary.minute_series) {
        const idx = Math.floor((row.minute_ms - startMs) / bucketMs);
        if (idx >= 0 && idx < buckets) data[idx] = row.cost_usd;
      }
      const labels = Array.from({ length: buckets }, (_, i) =>
        new Date(startMs + i * bucketMs).toLocaleTimeString(undefined, {
          hour12: false,
          hour: '2-digit',
          minute: '2-digit'
        })
      );
      chart.setOption({
        grid: { left: 8, right: 8, top: 8, bottom: 20, containLabel: true },
        xAxis: {
          type: 'category',
          data: labels,
          axisLabel: { color: '#8b949e', fontSize: 10, interval: 10 },
          axisLine: { lineStyle: { color: '#2a313c' } },
          axisTick: { show: false }
        },
        yAxis: {
          type: 'value',
          axisLabel: {
            color: '#8b949e',
            fontSize: 10,
            formatter: (v) => '$' + v.toFixed(v < 1 ? 3 : 2)
          },
          splitLine: { lineStyle: { color: '#1c2330' } }
        },
        tooltip: {
          trigger: 'axis',
          backgroundColor: '#161b22',
          borderColor: '#2a313c',
          textStyle: { color: '#e6edf3', fontSize: 12 },
          formatter: (p) => `${p[0].name}<br/>${fmtUsdSmall(p[0].value)}`
        },
        series: [
          {
            type: 'bar',
            data,
            itemStyle: { color: '#7c5cff', borderRadius: [2, 2, 0, 0] },
            barCategoryGap: '25%'
          }
        ]
      });
    }
  });

  // ---------- helpers ----------
  async function refreshAll() {
    try {
      const [s, c, p, m, ss] = await Promise.all([
        fetchSummary(),
        fetchCalls(20),
        fetchProviders(),
        fetchModels(),
        fetchSessions()
      ]);
      summary = s;
      calls = c.calls ?? [];
      providers = p.providers ?? [];
      models = m.models ?? [];
      sessions = ss.sessions ?? [];
    } catch (e) {
      console.error('refresh failed', e);
    }
  }

  function applySpend(row) {
    if (!row) return;
    // Optimistic tile bump for successful + in-window rows
    if (summary && row.completed && row.status_code < 400) {
      const cloned = JSON.parse(JSON.stringify(summary));
      for (const k of ['today', 'week', 'mtd']) {
        cloned[k].cost_usd = (cloned[k].cost_usd || 0) + (row.cost_usd || 0);
        cloned[k].requests = (cloned[k].requests || 0) + 1;
        cloned[k].input_tokens = (cloned[k].input_tokens || 0) + (row.input_tokens || 0);
        cloned[k].output_tokens = (cloned[k].output_tokens || 0) + (row.output_tokens || 0);
        cloned[k].cache_read_tokens = (cloned[k].cache_read_tokens || 0) + (row.cache_read_tokens || 0);
      }
      summary = cloned;
    }
    // Prepend to live tail
    calls = [row, ...calls.filter((c) => c.request_id !== row.request_id)].slice(0, 20);
    highlightIds.add(row.request_id);
    highlightIds = new Set(highlightIds);
    setTimeout(() => {
      highlightIds.delete(row.request_id);
      highlightIds = new Set(highlightIds);
    }, 1500);
  }

  // ---------- lifecycle ----------
  onMount(async () => {
    // Lazy-load echarts so the initial bundle stays small
    const echarts = await import('echarts');
    chart = echarts.init(chartEl, null, { renderer: 'canvas' });
    new ResizeObserver(() => chart?.resize()).observe(chartEl);

    await refreshAll();

    // SSE for live updates; slow safety-net poll for any missed messages
    eventSource = subscribe(
      (row) => {
        connected = true;
        applySpend(row);
      },
      () => {
        connected = false;
        setTimeout(refreshAll, 1000);
      }
    );
    eventSource.addEventListener('open', () => (connected = true));

    pollInterval = setInterval(refreshAll, 15000);
  });

  onDestroy(() => {
    eventSource?.close();
    clearInterval(pollInterval);
    chart?.dispose();
  });

  // ---------- derived ----------
  let mtdCost = $derived(summary?.mtd?.cost_usd ?? 1);
  function pct(v) {
    return mtdCost > 0 ? Math.min(100, (v / mtdCost) * 100) : 0;
  }
</script>

<header>
  <h1><span class="brand-glyph">◆</span>Headroom Token View</h1>
  <span class="live" class:on={connected}>
    <span class="dot"></span>
    {connected ? 'live · SSE connected' : 'connecting…'}
  </span>
</header>

<main>
  <div class="tiles">
    <div class="tile">
      <div class="label">Today</div>
      <div class="value">{fmtUsd(summary?.today?.cost_usd)}</div>
      <div class="sub">
        {fmtNum(summary?.today?.requests)} calls ·
        {fmtNum((summary?.today?.input_tokens ?? 0) + (summary?.today?.output_tokens ?? 0))} tokens
      </div>
    </div>
    <div class="tile">
      <div class="label">This Week</div>
      <div class="value">{fmtUsd(summary?.week?.cost_usd)}</div>
      <div class="sub">{fmtNum(summary?.week?.requests)} calls</div>
    </div>
    <div class="tile">
      <div class="label">Month to date</div>
      <div class="value">{fmtUsd(summary?.mtd?.cost_usd)}</div>
      <div class="sub">{fmtNum(summary?.mtd?.requests)} calls</div>
    </div>
    <div class="tile">
      <div class="label">Cache reads (mtd)</div>
      <div class="value">{fmtNum(summary?.mtd?.cache_read_tokens)}</div>
      <div class="sub">tokens served from cache</div>
    </div>
  </div>

  <div class="chart-wrap">
    <h3>Cost / minute · last hour</h3>
    <div bind:this={chartEl} style="width: 100%; height: 140px;"></div>
  </div>

  <div class="grid-3">
    <div class="panel">
      <h3>By provider</h3>
      {#if providers.length === 0}
        <div class="empty">no calls yet</div>
      {:else}
        {#each providers as p}
          <div>
            <div class="row">
              <span class="name">{p.provider}</span>
              <span class="meta">{fmtNum(p.requests)}</span>
              <span class="cost">{fmtUsdSmall(p.cost_usd)}</span>
            </div>
            <div class="bar-wrap"><div class="bar" style="width: {pct(p.cost_usd).toFixed(1)}%"></div></div>
          </div>
        {/each}
      {/if}
    </div>

    <div class="panel">
      <h3>By model</h3>
      {#if models.length === 0}
        <div class="empty">no calls yet</div>
      {:else}
        {#each models as m}
          <div>
            <div class="row">
              <span class="name" title={m.model}>{truncate(m.model, 30)}</span>
              <span class="meta">{fmtNum(m.requests)}</span>
              <span class="cost">{fmtUsdSmall(m.cost_usd)}</span>
            </div>
            <div class="bar-wrap"><div class="bar" style="width: {pct(m.cost_usd).toFixed(1)}%"></div></div>
          </div>
        {/each}
      {/if}
    </div>

    <div class="panel">
      <h3>By session</h3>
      {#if sessions.length === 0}
        <div class="empty">no sessions yet</div>
      {:else}
        {#each sessions as s}
          <div>
            <div class="row">
              <span class="name" title={s.session_id}>{truncate(s.session_id, 14)}</span>
              <span class="meta">{fmtNum(s.requests)}</span>
              <span class="cost">{fmtUsdSmall(s.cost_usd)}</span>
            </div>
            <div class="bar-wrap"><div class="bar" style="width: {pct(s.cost_usd).toFixed(1)}%"></div></div>
          </div>
        {/each}
      {/if}
    </div>
  </div>

  <div class="panel">
    <h3>Live tail · last 20</h3>
    <table>
      <thead>
        <tr>
          <th>ts</th><th>provider</th><th>model</th><th class="num">in&rarr;out</th>
          <th class="num">cost</th><th>session</th>
        </tr>
      </thead>
      <tbody>
        {#if calls.length === 0}
          <tr><td colspan="6" class="empty">no calls yet</td></tr>
        {:else}
          {#each calls as r (r.request_id)}
            <tr class:highlight={highlightIds.has(r.request_id)}>
              <td class="muted">{fmtTs(r.ts_ms)}</td>
              <td>{r.provider}</td>
              <td title={r.model}>{truncate(r.model, 26)}</td>
              <td class="num">{fmtNum(r.input_tokens)} → {fmtNum(r.output_tokens)}</td>
              <td class="num">{fmtUsdSmall(r.cost_usd)}</td>
              <td class="muted" title={r.session_id}>{truncate(r.session_id, 16)}</td>
            </tr>
          {/each}
        {/if}
      </tbody>
    </table>
  </div>

  <div class="footer">
    Point your apps at the proxy:
    <code>ANTHROPIC_BASE_URL=http://localhost:4000</code> ·
    <code>OPENAI_BASE_URL=http://localhost:4000/v1</code> ·
    <code>GOOGLE_BASE_URL=http://localhost:4000</code>
    · <a href="/api/health">/api/health</a>
  </div>
</main>

<style>
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 24px;
    border-bottom: 1px solid var(--border);
  }
  header h1 {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    letter-spacing: 0.2px;
  }
  header h1 .brand-glyph { color: var(--accent); margin-right: 6px; }
  .live { font-size: 11px; color: var(--text-dim); display: flex; align-items: center; gap: 6px; }
  .live .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-dim); transition: background 0.2s; }
  .live.on .dot {
    background: var(--good);
    box-shadow: 0 0 8px var(--good);
    animation: pulse 2s infinite;
  }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
  main { padding: 24px; max-width: 1200px; margin: 0 auto; }
  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 14px;
    margin-bottom: 24px;
  }
  .tile {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px;
  }
  .tile .label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--text-dim);
    margin-bottom: 8px;
  }
  .tile .value {
    font-size: 22px;
    font-weight: 600;
    font-family: var(--mono);
  }
  .tile .sub { font-size: 11px; color: var(--text-dim); margin-top: 4px; }
  .chart-wrap {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px;
    margin-bottom: 24px;
  }
  .chart-wrap h3,
  .panel h3 {
    margin: 0 0 10px 0;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--text-dim);
    font-weight: 500;
  }
  .grid-3 {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 14px;
    margin-bottom: 24px;
  }
  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px;
  }
  .row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 4px 0;
    font-family: var(--mono);
    font-size: 13px;
  }
  .row .name {
    color: var(--text);
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .row .meta { color: var(--text-dim); margin: 0 10px; font-size: 11px; }
  .row .cost { color: var(--accent-2); }
  .bar-wrap {
    background: var(--panel-2);
    border-radius: 4px;
    height: 4px;
    margin-top: 4px;
    overflow: hidden;
  }
  .bar { background: var(--accent); height: 100%; transition: width 0.3s; }
  table { width: 100%; font-family: var(--mono); font-size: 12px; border-collapse: collapse; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }
  th {
    color: var(--text-dim);
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    font-size: 10px;
  }
  td.num { text-align: right; }
  td.muted { color: var(--text-dim); }
  tr.highlight { animation: flash 1.5s ease-out; }
  @keyframes flash {
    0% { background: rgba(124, 92, 255, 0.18); }
    100% { background: transparent; }
  }
  .empty { color: var(--text-dim); padding: 8px 0; font-style: italic; }
  .footer {
    color: var(--text-dim);
    font-size: 11px;
    margin-top: 32px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
  }
</style>

<script>
  import { onMount, onDestroy } from 'svelte';
  import {
    fetchSummary, fetchCalls, fetchProviders, fetchModels, fetchSessions,
    fetchInsights, fetchLatency, fetchTools, fetchSessionDetail, subscribe
  } from '$lib/api.js';
  import { fmtUsd, fmtUsdSmall, fmtNum, fmtTs, truncate, midTruncate, fmtCost } from '$lib/format.js';

  // ---------- state ----------
  let summary = $state(null);
  let calls = $state([]);
  let providers = $state([]);
  let models = $state([]);
  let sessions = $state([]);
  let insights = $state([]);
  let totalSavings = $state(0);
  let latency = $state([]);
  let tools = $state([]);
  let toolTokensTotal = $state(0);
  let toolHotspot = $state(null);
  let connected = $state(false);
  let chartEl;
  let chart;          // ECharts instance
  let pollInterval;
  let eventSource;
  let highlightIds = $state(new Set());

  // session waterfall panel
  let waterfall = $state(null);     // { session_id, calls, summary, insights }
  let waterfallLoading = $state(false);

  // ---------- small formatters ----------
  const fmtMs = (ms) => (ms == null ? '—' : ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`);
  function tokPerSec(row) {
    if (!row || row.ttft_ms == null || !row.latency_ms || !row.output_tokens) return null;
    const genMs = Math.max(1, row.latency_ms - row.ttft_ms);
    return row.output_tokens / (genMs / 1000);
  }
  const sevColor = { win: '#3fb950', advice: '#7c5cff', info: '#58a6ff', warn: '#d29922' };

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
      const [s, c, p, m, ss, ins, lat, tl] = await Promise.all([
        fetchSummary(),
        fetchCalls(20),
        fetchProviders(),
        fetchModels(),
        fetchSessions(),
        fetchInsights(),
        fetchLatency(),
        fetchTools()
      ]);
      summary = s;
      calls = c.calls ?? [];
      providers = p.providers ?? [];
      models = m.models ?? [];
      sessions = ss.sessions ?? [];
      insights = ins.insights ?? [];
      totalSavings = ins.total_estimated_savings_usd ?? 0;
      latency = lat.models ?? [];
      tools = tl.tools ?? [];
      toolTokensTotal = tl.total_tool_tokens ?? 0;
      toolHotspot = tl.hotspot ?? null;
    } catch (e) {
      console.error('refresh failed', e);
    }
  }

  async function openWaterfall(sessionId) {
    waterfallLoading = true;
    waterfall = { session_id: sessionId, calls: [], summary: null, insights: [] };
    try {
      waterfall = await fetchSessionDetail(sessionId);
    } catch (e) {
      console.error('session detail failed', e);
    } finally {
      waterfallLoading = false;
    }
  }
  function closeWaterfall() {
    waterfall = null;
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
  let maxToolTokens = $derived(Math.max(...tools.map((t) => t.total_tokens || 0), 1));
  function toolPct(v) {
    return Math.min(100, ((v || 0) / maxToolTokens) * 100);
  }
</script>

<header>
  <h1><span class="brand-glyph">◆</span>tokview</h1>
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
      <div class="label">Month to date</div>
      <div class="value">{fmtUsd(summary?.mtd?.cost_usd)}</div>
      <div class="sub">{fmtNum(summary?.mtd?.requests)} calls</div>
    </div>
    <div class="tile accent">
      <div class="label">Tool tokens</div>
      <div class="value">{fmtNum(toolTokensTotal)}</div>
      <div class="sub">spent inside tool calls</div>
    </div>
    <div class="tile">
      <div class="label">Cache reads</div>
      <div class="value">{fmtNum(summary?.mtd?.cache_read_tokens)}</div>
      <div class="sub">tokens served from cache</div>
    </div>
  </div>

  <div class="chart-wrap">
    <h3>Cost / minute · last hour</h3>
    <div bind:this={chartEl} style="width: 100%; height: 140px;"></div>
  </div>

  {#if insights.length > 0}
    <div class="panel insights">
      <h3>
        Savings coach
        {#if totalSavings > 0}<span class="savings-pill">~{fmtUsd(totalSavings)} potential</span>{/if}
      </h3>
      <div class="cards">
        {#each insights as ins}
          <div class="card" style="--sev: {sevColor[ins.severity] ?? '#7c5cff'}">
            <div class="card-head">
              <span class="card-title">{ins.title}</span>
              {#if ins.estimated_savings_usd > 0}
                <span class="card-savings">{fmtUsd(ins.estimated_savings_usd)}</span>
              {/if}
            </div>
            <div class="card-detail">{ins.detail}</div>
          </div>
        {/each}
      </div>
    </div>
  {/if}

  <div class="panel hotspots">
    <h3>
      Tool hotspots <span class="hint">where your tokens actually go</span>
      {#if toolTokensTotal > 0}<span class="savings-pill alt">{fmtNum(toolTokensTotal)} tok</span>{/if}
    </h3>
    {#if toolHotspot}
      <div class="hotspot-callout">
        ⚠ <strong>{toolHotspot.tool_name}</strong> is {toolHotspot.share_pct}% of your tool tokens
        ({fmtNum(toolHotspot.total_tokens)}) — likely a large result re-sent across turns.
      </div>
    {/if}
    {#if tools.length === 0}
      <div class="empty">no tool calls recorded yet — run <code>tokview wrap claude</code> or <code>tokview wrap codex</code></div>
    {:else}
      <table>
        <thead>
          <tr><th>tool</th><th class="num">calls</th><th class="num">args</th><th class="num">results</th><th class="num">total</th><th>share</th></tr>
        </thead>
        <tbody>
          {#each tools as t}
            <tr>
              <td title={t.tool_name}>{truncate(t.tool_name, 34)}</td>
              <td class="num">{fmtNum(t.calls)}</td>
              <td class="num muted">{fmtNum(t.arg_tokens)}</td>
              <td class="num">{fmtNum(t.result_tokens)}</td>
              <td class="num"><strong>{fmtNum(t.total_tokens)}</strong></td>
              <td class="share-cell">
                <span class="tok-bar" style="width: {toolPct(t.total_tokens)}%"></span>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
      <div class="tools-note">Token estimates only — not provider-billed (cache discounts make per-tool dollars misleading).</div>
    {/if}
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
      <h3>By session <span class="hint">click to trace</span></h3>
      {#if sessions.length === 0}
        <div class="empty">no sessions yet</div>
      {:else}
        {#each sessions as s}
          <button class="row-btn" onclick={() => openWaterfall(s.session_id)}>
            <div class="row">
              <span class="name" title={s.session_id}>{midTruncate(s.session_id, 16, 8)}</span>
              <span class="meta">{fmtNum(s.requests)}</span>
              <span class="cost">{fmtUsdSmall(s.cost_usd)}</span>
            </div>
            <div class="bar-wrap"><div class="bar" style="width: {pct(s.cost_usd).toFixed(1)}%"></div></div>
          </button>
        {/each}
      {/if}
    </div>
  </div>

  {#if latency.length > 0}
    <div class="panel">
      <h3>Latency · month to date</h3>
      <table>
        <thead>
          <tr>
            <th>model</th><th class="num">calls</th>
            <th class="num">TTFT p50</th><th class="num">TTFT p95</th>
            <th class="num">total p50</th><th class="num">total p95</th>
            <th class="num">tok/s p50</th>
          </tr>
        </thead>
        <tbody>
          {#each latency as l}
            <tr>
              <td title={l.model}>{truncate(l.model, 28)}</td>
              <td class="num">{fmtNum(l.count)}</td>
              <td class="num">{fmtMs(l.ttft_p50)}</td>
              <td class="num">{fmtMs(l.ttft_p95)}</td>
              <td class="num">{fmtMs(l.latency_p50)}</td>
              <td class="num">{fmtMs(l.latency_p95)}</td>
              <td class="num">{l.tokens_per_sec_p50 ? l.tokens_per_sec_p50.toFixed(0) : '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  <div class="panel">
    <h3>Live tail · last 20</h3>
    <table>
      <thead>
        <tr>
          <th>ts</th><th>provider</th><th>model</th><th class="num">in&rarr;out</th>
          <th class="num">TTFT</th><th class="num">tok/s</th>
          <th class="num">cost</th><th>session</th>
        </tr>
      </thead>
      <tbody>
        {#if calls.length === 0}
          <tr><td colspan="8" class="empty">no calls yet</td></tr>
        {:else}
          {#each calls as r (r.request_id)}
            <tr class:highlight={highlightIds.has(r.request_id)}>
              <td class="muted">{fmtTs(r.ts_ms)}</td>
              <td>{r.provider}</td>
              <td title={r.model}>{truncate(r.model, 24)}</td>
              <td class="num">{fmtNum(r.input_tokens)} → {fmtNum(r.output_tokens)}</td>
              <td class="num muted">{fmtMs(r.ttft_ms)}</td>
              <td class="num muted">{tokPerSec(r) ? tokPerSec(r).toFixed(0) : '—'}</td>
              <td class="num">{fmtCost(r.cost_usd, r.cost_estimated)}</td>
              {#if r.session_id}
                <td class="muted link" title={r.session_id} role="button" tabindex="0"
                    onclick={() => openWaterfall(r.session_id)}
                    onkeydown={(e) => e.key === 'Enter' && openWaterfall(r.session_id)}>
                  {midTruncate(r.session_id, 14, 8)}
                </td>
              {:else}
                <td class="muted">—</td>
              {/if}
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

{#if waterfall}
  <div class="overlay" role="button" tabindex="0"
       onclick={closeWaterfall} onkeydown={(e) => e.key === 'Escape' && closeWaterfall()}>
    <!-- stop propagation so clicks inside the sheet don't close it -->
    <div class="sheet" role="dialog" aria-modal="true" tabindex="-1"
         onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
      <div class="sheet-head">
        <div>
          <div class="sheet-title">Session trace</div>
          <div class="sheet-sub" title={waterfall.session_id}>{midTruncate(waterfall.session_id, 28, 10)}</div>
        </div>
        <button class="close" onclick={closeWaterfall} aria-label="Close">✕</button>
      </div>

      {#if waterfallLoading}
        <div class="empty">loading…</div>
      {:else if !waterfall.summary}
        <div class="empty">no calls in this session</div>
      {:else}
        {@const sum = waterfall.summary}
        {@const span = Math.max(1, sum.span_ms)}
        <div class="sheet-stats">
          <div><span class="k">calls</span><span class="v">{fmtNum(sum.requests)}</span></div>
          <div><span class="k">cost</span><span class="v">{fmtUsd(sum.cost_usd)}</span></div>
          <div><span class="k">tokens</span><span class="v">{fmtNum(sum.input_tokens + sum.output_tokens)}</span></div>
          <div><span class="k">duration</span><span class="v">{fmtMs(sum.span_ms)}</span></div>
          {#if sum.errors > 0}<div><span class="k">errors</span><span class="v err">{sum.errors}</span></div>{/if}
        </div>

        <!-- waterfall: one row per call, bar positioned by start offset, width by latency -->
        <div class="waterfall">
          {#each waterfall.calls as c (c.request_id)}
            {@const start = (c.start_ms ?? c.ts_ms) - sum.first_ts_ms}
            {@const dur = c.latency_ms ?? 1}
            {@const leftPct = (start / span) * 100}
            {@const widthPct = Math.max(0.8, (dur / span) * 100)}
            {@const ttftPct = c.ttft_ms ? (c.ttft_ms / Math.max(1, dur)) * widthPct : 0}
            <div class="wf-row">
              <div class="wf-label" title={c.model}>{truncate(c.model, 22)}</div>
              <div class="wf-track">
                <div
                  class="wf-bar"
                  class:err={(c.status_code ?? 200) >= 400}
                  style="left: {leftPct}%; width: {widthPct}%;"
                  title={`${c.model}\n${fmtMs(c.latency_ms)} · TTFT ${fmtMs(c.ttft_ms)} · ${fmtUsdSmall(c.cost_usd)}`}
                >
                  {#if ttftPct > 0}<span class="wf-ttft" style="width: {(ttftPct / widthPct) * 100}%"></span>{/if}
                </div>
              </div>
              <div class="wf-cost">{fmtCost(c.cost_usd, c.cost_estimated)}</div>
            </div>
          {/each}
        </div>

        {#if waterfall.tools && waterfall.tools.length > 0}
          {@const maxTool = Math.max(...waterfall.tools.map((t) => t.total_tokens), 1)}
          <div class="tools-section">
            <h4>Tools used · token estimates</h4>
            <table class="tools-table">
              <thead>
                <tr><th>tool</th><th class="num">calls</th><th class="num">args</th><th class="num">results</th><th class="num">total tokens</th></tr>
              </thead>
              <tbody>
                {#each waterfall.tools as t}
                  <tr>
                    <td title={t.tool_name}>{truncate(t.tool_name, 30)}</td>
                    <td class="num">{fmtNum(t.calls)}</td>
                    <td class="num muted">{fmtNum(t.arg_tokens)}</td>
                    <td class="num">{fmtNum(t.result_tokens)}</td>
                    <td class="num">
                      <span class="tok-bar" style="width: {(t.total_tokens / maxTool) * 100}%"></span>
                      <span class="tok-val">{fmtNum(t.total_tokens)}</span>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
            <div class="tools-note">Estimated by tokenizing each tool's argument and result blocks — not provider-billed (cache discounts make per-tool cost meaningless).</div>
          </div>
        {/if}

        {#if waterfall.insights && waterfall.insights.length > 0}
          <div class="sheet-insights">
            {#each waterfall.insights as ins}
              <div class="card" style="--sev: {sevColor[ins.severity] ?? '#58a6ff'}">
                <div class="card-head">
                  <span class="card-title">{ins.title}</span>
                  {#if ins.estimated_savings_usd > 0}<span class="card-savings">{fmtUsd(ins.estimated_savings_usd)}</span>{/if}
                </div>
                <div class="card-detail">{ins.detail}</div>
              </div>
            {/each}
          </div>
        {/if}
      {/if}
    </div>
  </div>
{/if}

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

  /* ---- savings coach ---- */
  .insights { margin-bottom: 24px; }
  .insights h3 { display: flex; align-items: center; gap: 10px; }
  .savings-pill {
    background: rgba(63, 185, 80, 0.15);
    color: var(--good);
    border: 1px solid rgba(63, 185, 80, 0.3);
    border-radius: 999px;
    padding: 1px 10px;
    font-size: 11px;
    letter-spacing: 0;
    text-transform: none;
    font-family: var(--mono);
  }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }
  .card {
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-left: 3px solid var(--sev, var(--accent));
    border-radius: 8px;
    padding: 12px 14px;
  }
  .card-head { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; }
  .card-title { font-weight: 600; font-size: 13px; color: var(--text); }
  .card-savings { font-family: var(--mono); color: var(--good); font-size: 13px; white-space: nowrap; }
  .card-detail { font-size: 12px; color: var(--text-dim); margin-top: 6px; line-height: 1.45; }

  .hint { color: var(--text-dim); font-size: 10px; font-weight: 400; text-transform: none; letter-spacing: 0; opacity: 0.7; }

  /* accent tile (the differentiator metric) */
  .tile.accent { border-color: var(--accent); }
  .tile.accent .value { color: var(--accent); }

  /* tool hotspots panel */
  .hotspots { margin-bottom: 24px; }
  .hotspots h3 { display: flex; align-items: center; gap: 10px; }
  .savings-pill.alt {
    background: rgba(124, 92, 255, 0.15);
    color: var(--accent);
    border-color: rgba(124, 92, 255, 0.35);
  }
  .hotspot-callout {
    background: rgba(210, 153, 34, 0.12);
    border: 1px solid rgba(210, 153, 34, 0.3);
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 12px;
    color: var(--text);
    margin-bottom: 12px;
  }
  .hotspot-callout strong { color: var(--warn); }
  .share-cell { width: 30%; }
  .share-cell .tok-bar { position: static; transform: none; display: block; height: 8px; border-radius: 3px; background: var(--accent); }

  /* clickable session rows */
  .row-btn {
    display: block;
    width: 100%;
    background: none;
    border: none;
    padding: 0;
    margin: 0;
    text-align: left;
    cursor: pointer;
    color: inherit;
    font: inherit;
    border-radius: 6px;
  }
  .row-btn:hover { background: var(--panel-2); }
  td.link { cursor: pointer; }
  td.link:hover { color: var(--accent-2); text-decoration: underline; }

  /* ---- session waterfall sheet ---- */
  .overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    display: flex;
    justify-content: center;
    align-items: flex-start;
    padding: 5vh 16px;
    z-index: 50;
    backdrop-filter: blur(2px);
  }
  .sheet {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    width: 100%;
    max-width: 900px;
    max-height: 90vh;
    overflow-y: auto;
    padding: 20px 22px;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
  }
  .sheet-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
  .sheet-title { font-size: 15px; font-weight: 600; }
  .sheet-sub { font-family: var(--mono); font-size: 12px; color: var(--text-dim); margin-top: 2px; }
  .close {
    background: var(--panel-2);
    border: 1px solid var(--border);
    color: var(--text-dim);
    border-radius: 6px;
    width: 28px;
    height: 28px;
    cursor: pointer;
    font-size: 13px;
  }
  .close:hover { color: var(--text); }
  .sheet-stats { display: flex; flex-wrap: wrap; gap: 22px; margin-bottom: 18px; }
  .sheet-stats > div { display: flex; flex-direction: column; }
  .sheet-stats .k { font-size: 10px; text-transform: uppercase; letter-spacing: 0.6px; color: var(--text-dim); }
  .sheet-stats .v { font-family: var(--mono); font-size: 18px; font-weight: 600; }
  .sheet-stats .v.err { color: var(--bad); }

  .waterfall { display: flex; flex-direction: column; gap: 3px; }
  .wf-row { display: grid; grid-template-columns: 180px 1fr 70px; align-items: center; gap: 10px; }
  .wf-label {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text-dim);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .wf-track { position: relative; height: 16px; background: var(--panel-2); border-radius: 3px; }
  .wf-bar {
    position: absolute;
    top: 0;
    height: 100%;
    min-width: 2px;
    background: var(--accent);
    border-radius: 3px;
    overflow: hidden;
  }
  .wf-bar.err { background: var(--bad); }
  .wf-ttft { display: block; height: 100%; background: rgba(255, 255, 255, 0.25); }
  .wf-cost { font-family: var(--mono); font-size: 11px; color: var(--accent-2); text-align: right; }
  .sheet-insights { margin-top: 18px; display: flex; flex-direction: column; gap: 10px; }

  /* ---- per-tool token breakdown ---- */
  .tools-section { margin-top: 20px; }
  .tools-section h4 {
    margin: 0 0 8px 0;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--text-dim);
    font-weight: 500;
  }
  .tools-table { width: 100%; }
  .tools-table td.num { position: relative; }
  .tok-bar {
    position: absolute;
    left: 0;
    top: 50%;
    transform: translateY(-50%);
    height: 14px;
    background: rgba(124, 92, 255, 0.18);
    border-radius: 3px;
    z-index: 0;
  }
  .tok-val { position: relative; z-index: 1; }
  .tools-note { font-size: 10px; color: var(--text-dim); margin-top: 8px; font-style: italic; }
</style>

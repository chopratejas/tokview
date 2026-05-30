// API client. All endpoints are same-origin (FastAPI serves the SPA).

export async function fetchSummary() {
  const r = await fetch('/api/summary');
  if (!r.ok) throw new Error(`/api/summary -> ${r.status}`);
  return r.json();
}

export async function fetchCalls(limit = 20, since = null) {
  const url = since != null ? `/api/calls?limit=${limit}&since=${since}` : `/api/calls?limit=${limit}`;
  const r = await fetch(url);
  if (!r.ok) throw new Error(`/api/calls -> ${r.status}`);
  return r.json();
}

export async function fetchProviders() {
  const r = await fetch('/api/providers');
  if (!r.ok) throw new Error(`/api/providers -> ${r.status}`);
  return r.json();
}

export async function fetchModels(limit = 20) {
  const r = await fetch(`/api/models?limit=${limit}`);
  if (!r.ok) throw new Error(`/api/models -> ${r.status}`);
  return r.json();
}

export async function fetchSessions(limit = 20) {
  const r = await fetch(`/api/sessions?limit=${limit}`);
  if (!r.ok) throw new Error(`/api/sessions -> ${r.status}`);
  return r.json();
}

export async function fetchSessionDetail(sessionId) {
  const r = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
  if (!r.ok) throw new Error(`/api/sessions/${sessionId} -> ${r.status}`);
  return r.json();
}

export async function fetchInsights() {
  const r = await fetch('/api/insights');
  if (!r.ok) throw new Error(`/api/insights -> ${r.status}`);
  return r.json();
}

export async function fetchLatency() {
  const r = await fetch('/api/latency');
  if (!r.ok) throw new Error(`/api/latency -> ${r.status}`);
  return r.json();
}

export async function fetchTools() {
  const r = await fetch('/api/tools');
  if (!r.ok) throw new Error(`/api/tools -> ${r.status}`);
  return r.json();
}

// SSE subscription helper. Returns the EventSource so the caller can close().
export function subscribe(onSpend, onError) {
  const es = new EventSource('/api/events');
  es.addEventListener('spend', (e) => {
    try {
      onSpend(JSON.parse(e.data));
    } catch (err) {
      console.warn('bad spend event', err);
    }
  });
  if (onError) es.onerror = onError;
  return es;
}

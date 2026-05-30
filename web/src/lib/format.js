export const fmtUsd = (n) => '$' + (Number(n) || 0).toFixed(2);
export const fmtUsdSmall = (n) => '$' + (Number(n) || 0).toFixed(4);
export const fmtNum = (n) => (Number(n) || 0).toLocaleString();
export const fmtTs = (ms) =>
  new Date(ms).toLocaleTimeString(undefined, { hour12: false });
export const truncate = (s, n) => {
  if (!s) return '';
  return s.length > n ? s.slice(0, n) + '…' : s;
};

// Middle-truncate so the *distinguishing* tail stays visible. Session ids
// share long prefixes (e.g. "codex-openai-chatgpt-…"); plain head-truncation
// makes them all look identical, so keep head + tail.
export const midTruncate = (s, head = 14, tail = 8) => {
  if (!s) return '-';
  if (s.length <= head + tail + 1) return s;
  return s.slice(0, head) + '…' + s.slice(-tail);
};

// Cost with a leading ~ when it's an estimated equivalent (subscription traffic).
export const fmtCost = (n, estimated) => (estimated ? '~' : '') + fmtUsdSmall(n);

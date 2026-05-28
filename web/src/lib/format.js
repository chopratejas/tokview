export const fmtUsd = (n) => '$' + (Number(n) || 0).toFixed(2);
export const fmtUsdSmall = (n) => '$' + (Number(n) || 0).toFixed(4);
export const fmtNum = (n) => (Number(n) || 0).toLocaleString();
export const fmtTs = (ms) =>
  new Date(ms).toLocaleTimeString(undefined, { hour12: false });
export const truncate = (s, n) => {
  if (!s) return '';
  return s.length > n ? s.slice(0, n) + '…' : s;
};

/** Human-readable byte size (B → TB, one decimal above bytes) for reference and storage labels. */

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];

// WHY '—' for a non-finite input: a missing number must look distinct from a real size.
export function formatBytes(n: number): string {
  if (!Number.isFinite(n)) return '—';
  let v = n;
  let i = 0;
  while (v >= 1024 && i < UNITS.length - 1) {
    v /= 1024;
    i++;
  }
  return `${i === 0 ? v : v.toFixed(1)} ${UNITS[i]}`;
}

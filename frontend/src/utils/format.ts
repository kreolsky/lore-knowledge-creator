/**
 * Format an ISO datetime string to 'YYYY-MM-DD HH:MM' for display.
 *
 * Falls back to server-provided `*_fmt` fields when available;
 * this utility exists as a client-side fallback.
 */
export function formatDate(dateStr: string): string {
  const d = new Date(dateStr);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

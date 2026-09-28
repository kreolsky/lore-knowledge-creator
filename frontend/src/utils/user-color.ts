/** Deterministic per-user presence color (gutter bars + connected-user chips). */

// INVARIANT: color is derived purely on the frontend from user_id — no backend
// or profile color. Why: every client must independently agree on a user's color
// so a peer's gutter bar matches their name chip across browsers, with no round-trip.

// Distinct hues chosen for legible white text on top. Error-red is intentionally
// excluded so presence never reads as an error state.
export const PRESENCE_PALETTE = [
  '#2563eb', // blue
  '#7c3aed', // violet
  '#0891b2', // cyan
  '#059669', // emerald
  '#65a30d', // lime-green
  '#ca8a04', // amber
  '#ea580c', // orange
  '#db2777', // pink
  '#9333ea', // purple
  '#0d9488', // teal
] as const;

/** Stable hash → palette index. Same id always yields the same color. */
export function userColor(userId: string): string {
  let hash = 0;
  for (let i = 0; i < userId.length; i++) {
    hash = (hash * 31 + userId.charCodeAt(i)) | 0;
  }
  const index = Math.abs(hash) % PRESENCE_PALETTE.length;
  return PRESENCE_PALETTE[index];
}

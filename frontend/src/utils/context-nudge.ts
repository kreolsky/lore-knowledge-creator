/** The context-length nudge's threshold rule.
 *
 * The instance ships two dials through GET /chat/models `context_nudge` —
 * `floor_tokens` and `percents` (1–3 ascending integers) — and the CLIENT turns
 * them into token counts against the chat's live window W: the window exists
 * only in the turn's `context_usage` frame, the backend persists `used` alone
 * and the relay derives nothing (the transparent harness seam). The same rule
 * serves every window: the banner reacts to the percentages alone, and the
 * floor only drops a stop that lands under it. */

/** At most three stops per chat: the server refuses a longer `percents`, the
 * client keeps the contract even against a body that slipped through. */
const MAX_THRESHOLDS = 3;

/**
 * The nudge thresholds for a window: `t_i = p_i/100 · W`, rounded, kept only
 * when `t_i >= floor` (dropped, never lifted to the floor), de-duplicated,
 * ascending. [] when no window is known — nothing to
 * measure against, so the chat is never nudged.
 */
export function contextNudgeThresholds(
  contextWindow: number,
  floor: number,
  percents: number[],
): number[] {
  if (!(contextWindow > 0) || !Number.isFinite(contextWindow)) return [];
  const out = new Set<number>();
  for (const p of percents) {
    const t = Math.round((p / 100) * contextWindow);
    // WHY > 0: a zero stop is crossed by an empty chat (used = 0) and would
    // nudge before the first turn.
    if (t >= floor && t > 0) out.add(t);
  }
  return [...out].sort((a, b) => a - b).slice(0, MAX_THRESHOLDS);
}

/** k — how many thresholds the chat has already crossed (`t <= used`). The
 * banner is due while k exceeds the answered level; one answer sets level = k,
 * so a jump across several stops counts as several of the three. */
export function contextNudgeCrossed(thresholds: number[], used: number): number {
  return thresholds.filter(t => t <= used).length;
}

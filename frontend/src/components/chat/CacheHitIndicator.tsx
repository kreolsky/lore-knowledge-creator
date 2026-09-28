/** CacheHitIndicator — prompt-cache hit share of the LAST completed turn.
 *
 * A compact inline text indicator beside TokenUsageGauge: "⚡ {pct}%" — the share
 * of the last turn's prompt the provider served from its prompt cache,
 * `cacheRead / (cacheRead + uncachedInput)`, summed over every model request of
 * the turn (a tool loop is several requests). The color band is the INVERSE of
 * the gauge's: a low hit rate is the alarm — it means something re-tokenized the
 * cached prefix (a changed system prompt, a rewritten history node).
 *
 * The figure is READ from dsh's `turn-tail` node (`data.tokenUsage`, derived by
 * the assembler's deriveTurnTokenUsage), never summed here from per-step usage.
 * `cacheReadTokens` is present only when every request of the turn reported the
 * bucket — a provider without cache accounting yields no indicator at all, not a
 * misleading "0%".
 *
 * INVARIANT (project-wide): border-radius: 0 — no rounded corners anywhere.
 */
import { useTranslation } from '../../i18n';
import type { ConversationVM } from '../../store/chat-store/conversation-feed';

/** The last completed turn's cache accounting, or null when no turn reported it. */
export interface CacheHitFigure {
  cacheRead: number;
  uncached: number;
  pct: number;
}

interface TurnTailUsage {
  uncachedInputTokens?: unknown;
  cacheReadTokens?: unknown;
}

/** Resolve the figure from the published conversation: the LAST `turn-tail`
 * node whose tokenUsage carries a cacheReadTokens bucket. */
export function lastTurnCacheHit(conversation: readonly ConversationVM[]): CacheHitFigure | null {
  for (let i = conversation.length - 1; i >= 0; i--) {
    const node = conversation[i];
    if (node.kind !== 'turn-tail') continue;
    const usage = (node.data as { tokenUsage?: TurnTailUsage } | null)?.tokenUsage;
    const cacheRead = usage?.cacheReadTokens;
    const uncached = usage?.uncachedInputTokens;
    if (typeof cacheRead !== 'number' || typeof uncached !== 'number') return null;
    const prompt = cacheRead + uncached;
    if (prompt <= 0) return null;
    return { cacheRead, uncached, pct: Math.round((cacheRead / prompt) * 100) };
  }
  return null;
}

/** Color band from the hit percentage — LOW is the alarm here. */
function bandFor(pct: number): 'neutral' | 'amber' | 'danger' {
  if (pct < 40) return 'danger';
  if (pct < 80) return 'amber';
  return 'neutral';
}

const BAND_TEXT: Record<string, string> = {
  neutral: 'text-muted',
  amber: 'text-amber',
  danger: 'text-danger',
};

export function CacheHitIndicator({ figure }: { figure: CacheHitFigure }) {
  const { t } = useTranslation();
  const band = bandFor(figure.pct);
  return (
    <span
      data-band={band}
      className={`shrink-0 text-ui-xs leading-none tabular-nums ${BAND_TEXT[band]}`}
      title={t('chatCacheHitTip', {
        cached: String(figure.cacheRead), fresh: String(figure.uncached), pct: figure.pct,
      })}
      aria-label={t('chatCacheHitLabel')}
    >
      <span className="sr-only">{t('chatCacheHitLabel')}: </span>
      ⚡ {figure.pct}%
    </span>
  );
}

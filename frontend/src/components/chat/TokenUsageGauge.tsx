/** TokenUsageGauge — context-window fill indicator.
 *
 * A COMPACT INLINE text indicator placed on the same row as the context picker +
 * persona dropdown (right side): "{used} / {cap} · {pct}%". The text COLOR shifts
 * neutral → amber → danger as usage nears the cap (the fill level is also explicit
 * in the percentage). No bar/track — a thin empty track reads as a stray line, so
 * the gauge is text-only.
 *
 * INVARIANT (project-wide): border-radius: 0 — no rounded corners anywhere.
 * The gauge is AI-chat only (gated on activeSessionId in ChatInput); note chats
 * never render it.
 */
import { useTranslation } from '../../i18n';

interface Props {
  used: number;
  cap: number;
}

/** Humanize a token count: <1000 as-is, else "N.Nk" (or rounded "Nk" at ≥100k).
 * Token convention: 1k = 1000 tokens (not 1024). */
function humanizeTokens(n: number): string {
  if (n < 1000) return String(n);
  const k = n / 1000;
  if (k >= 100) return `${Math.round(k)}k`;
  // toFixed(1) then strip a trailing ".0" so 1.0k → 1k, 12.3k stays 12.3k.
  const s = k.toFixed(1);
  return `${s.endsWith('.0') ? s.slice(0, -2) : s}k`;
}

/** Determine the color band from the fill percentage. */
function bandFor(pct: number): 'neutral' | 'amber' | 'danger' {
  if (pct > 90) return 'danger';
  if (pct >= 70) return 'amber';
  return 'neutral';
}

const BAND_TEXT: Record<string, string> = {
  neutral: 'text-muted',
  amber: 'text-amber',
  danger: 'text-danger',
};

export function TokenUsageGauge({ used, cap }: Props) {
  const { t } = useTranslation();
  const safeCap = cap > 0 ? cap : 1;
  // Clamp at 100%: a used value over the cap shows 100% (never implies >100% fill).
  const pct = Math.min(100, Math.max(0, Math.round((used / safeCap) * 100)));
  const band = bandFor(pct);

  return (
    <span
      data-band={band}
      className={`shrink-0 text-ui-xs leading-none tabular-nums ${BAND_TEXT[band]}`}
      title={t('chatContextTokensTip', { used: String(used), cap: String(cap), pct })}
      aria-label={t('chatContextTokensLabel')}
    >
      <span className="sr-only">{t('chatContextTokensLabel')}: </span>
      {humanizeTokens(used)} / {humanizeTokens(cap)} · {pct}%
    </span>
  );
}

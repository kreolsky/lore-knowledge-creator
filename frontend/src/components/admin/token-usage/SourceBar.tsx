/** Token-usage source split — one 100% bar by `source` with numbers, and the line naming what the ledger does not count. */

import { useTranslation } from '../../../i18n';
import type { TranslationKey } from '../../../i18n/en';
import { compact, slotColor, type Ranked } from './pivot';

// Fixed source order → fixed colour slot, so a source keeps its colour whatever the filter.
const SOURCES: { id: string; label: TranslationKey }[] = [
  { id: 'chat', label: 'tokenUsageSrcChat' },
  { id: 'extractor', label: 'tokenUsageSrcExtractor' },
  { id: 'digest', label: 'tokenUsageSrcDigest' },
  { id: 'chat_topics', label: 'tokenUsageSrcChatTopics' },
  { id: 'image_refine', label: 'tokenUsageSrcImageRefine' },
  { id: 'unknown', label: 'tokenUsageSrcUnknown' },
];

export function SourceBar({ items, total }: { items: Ranked[]; total: number }) {
  const { t } = useTranslation();
  const known = new Map(SOURCES.map((s, i) => [s.id, { i, label: s.label }]));
  // `unknown` (and any source this build does not know) wears the neutral colour.
  const colorOf = (id: string) => (id === 'unknown' ? slotColor(-1) : slotColor(known.get(id)?.i ?? -1));
  const labelOf = (id: string) => (known.has(id) ? t(known.get(id)!.label) : id);
  if (total <= 0) return null;
  const ordered = [...items].sort((a, b) => (known.get(a.key)?.i ?? 99) - (known.get(b.key)?.i ?? 99));

  return (
    <section className="bg-surface2 p-3 space-y-2">
      <h3 className="text-xs text-text-dim">{t('tokenUsageSources')}</h3>
      <div className="flex h-3 w-full gap-[2px]">
        {ordered.map(s => (
          <div key={s.key} title={`${labelOf(s.key)}: ${s.value.toLocaleString()}`} style={{ width: `${(s.value / total) * 100}%`, background: colorOf(s.key) }} />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-text-muted">
        {ordered.map(s => (
          <span key={s.key} className="inline-flex items-center gap-1.5">
            <span className="inline-block w-2.5 h-2.5" style={{ background: colorOf(s.key) }} />
            {labelOf(s.key)}
            <span className="tabular-nums text-text">{compact(s.value)}</span>
            <span className="tabular-nums text-text-dim">{((s.value / total) * 100).toFixed(1)}%</span>
          </span>
        ))}
      </div>
      <p className="text-xs text-text-dim">{t('tokenUsageNotCounted')}</p>
    </section>
  );
}

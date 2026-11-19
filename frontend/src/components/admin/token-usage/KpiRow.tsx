/** Token-usage KPI tiles — each value with its Δ% against the previous equal-length period ("—" when that period is not comparable). */

import { useTranslation } from '../../../i18n';
import type { TranslationKey } from '../../../i18n/en';
import { compact, type Kpis } from './pivot';

interface KpiRowProps {
  current: Kpis | null;
  /** null = the previous period is empty or starts before the oldest recorded day. */
  previous: Kpis | null;
  loading: boolean;
}

type Tile = { key: keyof Kpis; label: TranslationKey; fmt: (v: number) => string };

const TILES: Tile[] = [
  { key: 'total', label: 'tokenUsageTotal', fmt: compact },
  { key: 'input', label: 'tokenUsageUncachedInput', fmt: compact },
  { key: 'output', label: 'tokenUsageOutput', fmt: compact },
  { key: 'cacheHit', label: 'tokenUsageCacheHit', fmt: v => `${(v * 100).toFixed(1)}%` },
  { key: 'activeUsers', label: 'tokenUsageActiveUsers', fmt: v => v.toLocaleString() },
];

function delta(cur: number | null, prev: number | null | undefined): string {
  if (cur === null || prev === null || prev === undefined || prev === 0) return '—';
  const pct = ((cur - prev) / prev) * 100;
  return `${pct > 0 ? '+' : ''}${pct.toFixed(Math.abs(pct) < 10 ? 1 : 0)}%`;
}

export function KpiRow({ current, previous, loading }: KpiRowProps) {
  const { t } = useTranslation();
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2">
      {TILES.map(tile => {
        const v = current ? current[tile.key] : null;
        return (
          <div key={tile.key} className="bg-surface2 p-3 min-w-0">
            <div className="text-xs text-text-dim mb-1 truncate">{t(tile.label)}</div>
            <div className="text-ui-xl tabular-nums" data-token-kpi={tile.key}>
              {loading ? '…' : v === null ? '—' : tile.fmt(v)}
            </div>
            <div className="text-xs text-text-dim mt-1 tabular-nums" title={t('tokenUsageVsPrevious')}>
              {loading ? '' : delta(v, previous ? previous[tile.key] : null)}
            </div>
          </div>
        );
      })}
    </div>
  );
}

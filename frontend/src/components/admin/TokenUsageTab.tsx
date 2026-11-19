/** Admin token-usage dashboard — where the period's LLM tokens went: KPIs, daily chart, user↔model drill-down, sources.
 *
 * State and fetch only; the cube is pivoted in token-usage/pivot.ts and rendered
 * by token-usage/*. One GET /api/admin/token-usage (see SYSTEM: token-usage) per
 * period plus one for the equal-length period before it (the KPI Δ). Every filter
 * chip, axis flip and metric switch re-slices the same rows with no request.
 * A failed load shows a red line + toast and clears every block — never the
 * previous period's numbers under the new one. Days are UTC calendar days.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { X } from 'lucide-react';
import { Button, CenterHeading, DateField, SegmentedControl } from '../ui';
import { getTokenUsage, type TokenUsageCube, type TokenUsageCubeRow } from '../../api/admin';
import { useAppStore } from '../../store/app-store';
import { useTranslation } from '../../i18n';
import type { TranslationKey } from '../../i18n/en';
import {
  DAY_MS, NO_GROUP_KEY, OTHER_KEY, SERIES_SLOTS, SYSTEM_KEY,
  applyFilters, dailyStacks, dayMs, daysInRange, keysOf, kpis, membershipOf, previousRange,
  rangeDays, rank, slotColor, totalOf, utcDay,
  type Filter, type FilterKind, type Metric, type Subject,
} from './token-usage/pivot';
import { KpiRow } from './token-usage/KpiRow';
import { DailyChart } from './token-usage/DailyChart';
import { RankPanel } from './token-usage/RankPanel';
import { SourceBar } from './token-usage/SourceBar';

const MAX_RANGE_DAYS = 366;
type Preset = '7' | '30' | '90' | 'this' | 'last' | 'custom';
type StackBy = 'model' | 'subject';

const PRESETS: { value: Preset; label: TranslationKey }[] = [
  { value: '7', label: 'tokenUsagePreset7' },
  { value: '30', label: 'tokenUsagePreset30' },
  { value: '90', label: 'tokenUsagePreset90' },
  { value: 'this', label: 'tokenUsageThisMonth' },
  { value: 'last', label: 'tokenUsageLastMonth' },
  { value: 'custom', label: 'tokenUsageCustom' },
];
const METRICS: { value: Metric; label: TranslationKey }[] = [
  { value: 'total', label: 'tokenUsageTotal' },
  { value: 'input', label: 'tokenUsageInput' },
  { value: 'output', label: 'tokenUsageOutput' },
  { value: 'cache_read', label: 'tokenUsageCacheRead' },
  { value: 'cache_write', label: 'tokenUsageCacheWrite' },
];
const KIND_LABEL: Record<FilterKind, TranslationKey> = {
  user: 'tokenUsageFilterUser',
  group: 'tokenUsageFilterGroup',
  model: 'tokenUsageFilterModel',
};
const CSV_COLUMNS = ['input', 'output', 'cache_read', 'cache_write'] as const;

function presetRange(p: Exclude<Preset, 'custom'>): { from: string; to: string } {
  const now = Date.now();
  const today = utcDay(now);
  const d = new Date(now);
  const table: Record<Exclude<Preset, 'custom'>, () => { from: string; to: string }> = {
    '7': () => ({ from: utcDay(now - 6 * DAY_MS), to: today }),
    '30': () => ({ from: utcDay(now - 29 * DAY_MS), to: today }),
    '90': () => ({ from: utcDay(now - 89 * DAY_MS), to: today }),
    this: () => ({ from: utcDay(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1)), to: today }),
    last: () => ({
      from: utcDay(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() - 1, 1)),
      to: utcDay(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1) - DAY_MS),
    }),
  };
  return table[p]();
}

function csvCell(v: string | number): string {
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function downloadCsv(name: string, lines: (string | number)[][]): void {
  const url = URL.createObjectURL(new Blob([lines.map(l => l.map(csvCell).join(',')).join('\n')], { type: 'text/csv' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

interface Loaded { cube: TokenUsageCube; prev: TokenUsageCube; prevFrom: string }

export function TokenUsageTab() {
  const { t } = useTranslation();
  const showToast = useAppStore(s => s.showToast);
  const [preset, setPreset] = useState<Preset>('30');
  const [custom, setCustom] = useState(() => presetRange('30'));
  const [subject, setSubject] = useState<Subject>('user');
  const [metric, setMetric] = useState<Metric>('total');
  const [stackBy, setStackBy] = useState<StackBy>('model');
  const [filters, setFilters] = useState<Filter[]>([]);
  const [data, setData] = useState<Loaded | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [loading, setLoading] = useState(false);
  const seq = useRef(0);
  // WHY: `t` is a new function per language — in the fetch effect's deps it would refetch.
  const tRef = useRef(t);
  tRef.current = t;

  const { from, to } = preset === 'custom' ? custom : presetRange(preset);
  const days = rangeDays(from, to);
  const rangeValid = days >= 1 && days <= MAX_RANGE_DAYS;

  useEffect(() => {
    const id = ++seq.current;
    if (!rangeValid) {
      // WHY: a stale screen under an invalid range would read as that range's numbers.
      setData(null);
      return;
    }
    const prevRange = previousRange(from, to);
    setLoading(true);
    setData(null);
    Promise.all([getTokenUsage(from, to), getTokenUsage(prevRange.from, prevRange.to)])
      .then(([cube, prev]) => {
        if (id !== seq.current) return;
        setData({ cube, prev, prevFrom: prevRange.from });
        setLoadFailed(false);
      })
      .catch(() => {
        if (id !== seq.current) return;
        setLoadFailed(true);
        showToast(tRef.current('tokenUsageLoadFailed'), 'error');
      })
      .finally(() => { if (id === seq.current) setLoading(false); });
  }, [from, to, rangeValid, showToast]);

  const memberOf = useMemo(() => (data ? membershipOf(data.cube) : () => [] as string[]), [data]);
  const rows = useMemo(() => (data ? applyFilters(data.cube.rows, filters, memberOf) : []), [data, filters, memberOf]);

  const labelOf = useCallback((kind: FilterKind, key: string): string => {
    const fixed: Record<string, TranslationKey> = { [SYSTEM_KEY]: 'tokenUsageSystem', [NO_GROUP_KEY]: 'tokenUsageNoGroup', [OTHER_KEY]: 'tokenUsageOther' };
    if (fixed[key]) return t(fixed[key]);
    if (kind === 'user') return data?.cube.users[key] ?? key;
    if (kind === 'group') return data?.cube.groups[key]?.name ?? key;
    return key;
  }, [data, t]);

  const view = useMemo(() => {
    if (!data) return null;
    const prevRows = applyFilters(data.prev.rows, filters, membershipOf(data.prev));
    const comparable = !!data.cube.oldest_day && data.prevFrom >= data.cube.oldest_day && prevRows.length > 0;
    const subjectKeys = keysOf(subject, memberOf);
    const modelKeys = keysOf('model', memberOf);
    const stackDim: FilterKind = stackBy === 'model' ? 'model' : subject;
    const stackKeys = keysOf(stackDim, memberOf);
    // WHY: series colours are ranked over the UNFILTERED period, so a filter chip never
    // repaints a survivor — colour follows the entity, not its rank in the narrowed view.
    const slots = rank(data.cube.rows, stackKeys, metric).slice(0, SERIES_SLOTS).map(r => r.key);
    const stacks = dailyStacks(rows, daysInRange(from, to), stackKeys, slots, metric);
    const present = new Set(stacks.flatMap(s => s.parts.map(p => p.key)));
    const series = [...slots, OTHER_KEY].filter(k => present.has(k)).map(k => ({
      key: k, label: labelOf(stackDim, k), color: slotColor(slots.indexOf(k)),
    }));
    return {
      kpis: kpis(rows),
      prevKpis: comparable ? kpis(prevRows) : null,
      total: totalOf(rows, metric),
      stacks, series, stackDim, slots, subjectKeys, modelKeys,
      bySubject: rank(rows, subjectKeys, metric),
      byModel: rank(rows, modelKeys, metric),
      bySource: rank(rows, r => [r.source], metric),
    };
  }, [data, rows, filters, subject, stackBy, metric, memberOf, from, to, labelOf]);

  const addFilter = (kind: FilterKind, id: string) =>
    setFilters(prev => (prev.some(f => f.kind === kind && f.id === id) ? prev : [...prev, { kind, id }]));
  const removeFilter = (f: Filter) => setFilters(prev => prev.filter(x => x !== f));

  const onPreset = (p: Preset) => {
    if (p === 'custom') setCustom({ from, to });
    setPreset(p);
  };
  const onSubject = (s: Subject) => {
    setSubject(s);
    // WHY: a user chip and a group chip mean different slices; keep only the chips the new axis can show.
    setFilters(prev => prev.filter(f => f.kind === 'model' || f.kind === s));
  };

  const exportCsv = () => {
    if (!data) return;
    const header = [t('tokenUsageDay'), t('tokenUsageUser'), t('tokenUsageModel'), t('tokenUsageSource'),
      ...CSV_COLUMNS.map(c => t(METRICS.find(m => m.value === c)!.label))];
    const line = (r: TokenUsageCubeRow) => [r.day, labelOf('user', r.user_id ?? SYSTEM_KEY), r.model, r.source, ...CSV_COLUMNS.map(c => r[c])];
    downloadCsv(`token-usage-${from}-${to}.csv`, [header, ...rows.map(line)]);
  };

  const subjectTitle = t(subject === 'user' ? 'tokenUsageByUser' : 'tokenUsageByGroup');
  const colorOfStack = (dim: FilterKind) => (key: string) =>
    view && view.stackDim === dim ? slotColor(view.slots.indexOf(key)) : undefined;
  const empty = !!data && rows.length === 0;

  return (
    <div className="space-y-4">
      <CenterHeading>{t('adminTokenUsage')}</CenterHeading>

      <div className="flex flex-wrap items-center gap-2">
        <SegmentedControl ariaLabel={t('tokenUsagePeriod')} value={preset} onChange={v => onPreset(v as Preset)}
          options={PRESETS.map(p => ({ value: p.value, label: t(p.label) }))} />
        {preset === 'custom' && (
          <div className="flex items-center gap-1">
            <DateField ariaLabel={t('tokenUsageFrom')} value={custom.from} max={custom.to}
              min={utcDay(dayMs(custom.to) - (MAX_RANGE_DAYS - 1) * DAY_MS)}
              onChange={v => setCustom(c => ({ ...c, from: v }))} />
            <span className="text-text-dim">—</span>
            <DateField ariaLabel={t('tokenUsageTo')} value={custom.to} min={custom.from}
              max={utcDay(dayMs(custom.from) + (MAX_RANGE_DAYS - 1) * DAY_MS)}
              onChange={v => setCustom(c => ({ ...c, to: v }))} />
          </div>
        )}
        <SegmentedControl ariaLabel={t('tokenUsageSubject')} value={subject} onChange={v => onSubject(v as Subject)}
          options={[{ value: 'user', label: t('tokenUsageUsers') }, { value: 'group', label: t('tokenUsageGroups') }]} />
        <Button onClick={exportCsv} disabled={!data || empty}>{t('tokenUsageExportCsv')}</Button>
        <span className="text-xs text-text-dim">{t('tokenUsageUtcNote')}</span>
      </div>

      {filters.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          {filters.map(f => (
            <Button key={`${f.kind}:${f.id}`} variant="subtle" size="sm" onClick={() => removeFilter(f)} title={t('tokenUsageRemoveFilter')}>
              {t(KIND_LABEL[f.kind])}: {labelOf(f.kind, f.id)} <X size={12} />
            </Button>
          ))}
        </div>
      )}

      {!rangeValid && <p className="text-xs text-red">{t('tokenUsageRangeInvalid', { max: MAX_RANGE_DAYS })}</p>}
      {loadFailed && !loading && <p className="text-xs text-red" data-token-usage-error>{t('tokenUsageLoadFailed')}</p>}

      <KpiRow current={view?.kpis ?? null} previous={view?.prevKpis ?? null} loading={loading} />

      {empty && (
        <div className="bg-surface2 p-6 text-center text-sm text-text-dim" data-token-usage-empty>{t('tokenUsageEmpty')}</div>
      )}
      {loading && <div className="bg-surface2 p-6 text-center text-xs text-text-dim">{t('loading')}</div>}

      {view && !empty && (
        <>
          <section className="bg-surface2 p-3 space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-xs text-text-dim">{t('tokenUsageDaily')}</h3>
              <div className="flex flex-wrap items-center gap-2">
                <SegmentedControl ariaLabel={t('tokenUsageMetric')} value={metric} onChange={v => setMetric(v as Metric)}
                  options={METRICS.map(m => ({ value: m.value, label: t(m.label) }))} />
                <SegmentedControl ariaLabel={t('tokenUsageStackBy')} value={stackBy} onChange={v => setStackBy(v as StackBy)}
                  options={[{ value: 'model', label: t('tokenUsageModels') }, { value: 'subject', label: t(subject === 'user' ? 'tokenUsageUsers' : 'tokenUsageGroups') }]} />
              </div>
            </div>
            <DailyChart stacks={view.stacks} series={view.series} />
          </section>

          <div className="grid grid-cols-1 min-[900px]:grid-cols-2 gap-2">
            <RankPanel
              title={subjectTitle}
              items={view.bySubject}
              total={view.total}
              labelOf={k => labelOf(subject, k)}
              colorOf={colorOfStack(subject)}
              onPick={k => addFilter(subject, k)}
              split={k => rank(rows.filter(r => view.subjectKeys(r).includes(k)), view.modelKeys, metric)}
              splitLabelOf={k => labelOf('model', k)}
              onPickSplit={k => addFilter('model', k)}
              note={subject === 'group' ? t('tokenUsageGroupNote') : undefined}
            />
            <RankPanel
              title={t('tokenUsageByModel')}
              items={view.byModel}
              total={view.total}
              labelOf={k => labelOf('model', k)}
              colorOf={colorOfStack('model')}
              onPick={k => addFilter('model', k)}
              split={k => rank(rows.filter(r => r.model === k), view.subjectKeys, metric)}
              splitLabelOf={k => labelOf(subject, k)}
              onPickSplit={k => addFilter(subject, k)}
            />
          </div>

          <SourceBar items={view.bySource} total={view.total} />
        </>
      )}
    </div>
  );
}

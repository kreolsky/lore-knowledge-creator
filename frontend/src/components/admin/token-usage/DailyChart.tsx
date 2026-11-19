/** Token-usage daily chart — stacked bars per UTC day in inline SVG, a legend above and a per-day hover tooltip.
 *
 * Render only: stacks and series colours come from the tab (pivot.ts). Width
 * follows the container (ResizeObserver); a day without spend is an empty slot.
 * The hit target is the whole day column, not the bar.
 */

import { useEffect, useRef, useState } from 'react';
import { useTranslation } from '../../../i18n';
import { compact, type DayStack } from './pivot';

export interface SeriesMeta { key: string; label: string; color: string }

interface DailyChartProps {
  stacks: DayStack[];
  /** Series in stacking order (bottom first), OTHER last when present. */
  series: SeriesMeta[];
}

const HEIGHT = 220;
const PAD_LEFT = 44;
const PAD_BOTTOM = 20;
const PAD_TOP = 8;
const SEGMENT_GAP = 2;

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  const step = [1, 2, 2.5, 5, 10].find(s => s * p >= v) ?? 10;
  return step * p;
}

function useWidth<T extends HTMLElement>(): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(entries => setWidth(entries[0].contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

export function DailyChart({ stacks, series }: DailyChartProps) {
  const { t } = useTranslation();
  const [boxRef, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const meta = new Map(series.map(s => [s.key, s]));

  const plotW = Math.max(0, width - PAD_LEFT);
  const plotH = HEIGHT - PAD_TOP - PAD_BOTTOM;
  const slot = stacks.length ? plotW / stacks.length : 0;
  const barW = Math.max(1, Math.min(slot - SEGMENT_GAP, 28));
  const max = niceMax(Math.max(0, ...stacks.map(s => s.total)));
  const y = (v: number) => (v / max) * plotH;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map(f => f * max);
  const labelEvery = Math.max(1, Math.ceil(44 / Math.max(slot, 1)));

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const x = e.clientX - e.currentTarget.getBoundingClientRect().left - PAD_LEFT;
    const i = Math.floor(x / slot);
    setHover(i >= 0 && i < stacks.length ? i : null);
  };

  const hovered = hover !== null ? stacks[hover] : null;
  const tipLeft = hover !== null ? PAD_LEFT + hover * slot + slot / 2 : 0;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-text-muted">
        {series.map(s => (
          <span key={s.key} className="inline-flex items-center gap-1.5 min-w-0">
            <span className="inline-block w-2.5 h-2.5 shrink-0" style={{ background: s.color }} />
            <span className="truncate max-w-[14rem]">{s.label}</span>
          </span>
        ))}
      </div>
      <div ref={boxRef} className="relative w-full">
        {width > 0 && (
          <svg
            width={width}
            height={HEIGHT}
            role="img"
            aria-label={t('tokenUsageDaily')}
            onMouseMove={onMove}
            onMouseLeave={() => setHover(null)}
            className="block"
          >
            {ticks.map(v => (
              <g key={v}>
                <line x1={PAD_LEFT} x2={width} y1={PAD_TOP + plotH - y(v)} y2={PAD_TOP + plotH - y(v)} className="stroke-border" strokeWidth={1} />
                <text x={PAD_LEFT - 6} y={PAD_TOP + plotH - y(v)} dy="0.32em" textAnchor="end" className="fill-text-dim text-ui-2xs tabular-nums">
                  {compact(v)}
                </text>
              </g>
            ))}
            {hover !== null && (
              <rect x={PAD_LEFT + hover * slot} y={PAD_TOP} width={slot} height={plotH} className="fill-surface3" opacity={0.5} />
            )}
            {stacks.map((s, i) => {
              const x = PAD_LEFT + i * slot + (slot - barW) / 2;
              let base = PAD_TOP + plotH;
              return (
                <g key={s.day}>
                  {s.parts.map(p => {
                    const h = y(p.value);
                    const drawn = h > SEGMENT_GAP * 2 ? h - SEGMENT_GAP : h;
                    base -= h;
                    return <rect key={p.key} x={x} y={base + (h - drawn)} width={barW} height={Math.max(drawn, 0.5)} style={{ fill: meta.get(p.key)?.color }} />;
                  })}
                  {i % labelEvery === 0 && (
                    <text x={PAD_LEFT + i * slot + slot / 2} y={HEIGHT - 6} textAnchor="middle" className="fill-text-dim text-ui-2xs tabular-nums">
                      {s.day.slice(5)}
                    </text>
                  )}
                </g>
              );
            })}
          </svg>
        )}
        {hovered && (
          <div
            className="absolute top-0 z-10 pointer-events-none bg-surface border border-border shadow-lg p-2 text-xs min-w-[11rem]"
            style={{ left: tipLeft, transform: `translateX(${tipLeft > width / 2 ? '-105%' : '5%'})` }}
          >
            <div className="flex justify-between gap-3 mb-1 text-text">
              <span className="tabular-nums">{hovered.day}</span>
              <span className="tabular-nums font-medium">{hovered.total.toLocaleString()}</span>
            </div>
            {hovered.parts.length === 0 && <div className="text-text-dim">{t('tokenUsageNoSpend')}</div>}
            {[...hovered.parts].reverse().map(p => (
              <div key={p.key} className="flex items-center gap-1.5 text-text-muted">
                <span className="inline-block w-2 h-2 shrink-0" style={{ background: meta.get(p.key)?.color }} />
                <span className="truncate flex-1">{meta.get(p.key)?.label ?? p.key}</span>
                <span className="tabular-nums">{p.value.toLocaleString()}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

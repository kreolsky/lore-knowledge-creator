/** Token-usage ranked panel — name · total · share bar per row; a chevron expands the cross split, a name click filters the screen. */

import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { Button, IconButton } from '../../ui';
import { useTranslation } from '../../../i18n';
import { compact, type Ranked } from './pivot';

interface RankPanelProps {
  title: string;
  items: Ranked[];
  /** The share-bar denominator (the period's total for the metric). */
  total: number;
  labelOf: (key: string) => string;
  /** Chart colour of a row, when this panel's dimension is the one the chart stacks by. */
  colorOf?: (key: string) => string | undefined;
  onPick: (key: string) => void;
  split: (key: string) => Ranked[];
  splitLabelOf: (key: string) => string;
  onPickSplit: (key: string) => void;
  note?: string;
}

function Row({ label, value, share, color, onPick, indent }: {
  label: string; value: number; share: number; color?: string; onPick: () => void; indent?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div className={`flex items-center gap-2 min-w-0 ${indent ? 'pl-7' : ''}`}>
      {color && <span className="inline-block w-2.5 h-2.5 shrink-0" style={{ background: color }} />}
      <div className="min-w-0 flex-1 [&>button]:max-w-full [&>button]:overflow-hidden">
        <Button variant="ghost" size="sm" onClick={onPick} title={t('tokenUsageFilterBy', { name: label })}>
          <span className="truncate">{label}</span>
        </Button>
      </div>
      <span className="w-14 text-right text-xs tabular-nums text-text" title={value.toLocaleString()}>{compact(value)}</span>
      <div className="w-20 sm:w-28 h-2 bg-surface3 shrink-0" title={`${(share * 100).toFixed(1)}%`}>
        <div className="h-full bg-accent" style={{ width: `${Math.min(100, share * 100)}%` }} />
      </div>
    </div>
  );
}

export function RankPanel({ title, items, total, labelOf, colorOf, onPick, split, splitLabelOf, onPickSplit, note }: RankPanelProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState<Set<string>>(new Set());
  const toggle = (key: string) =>
    setOpen(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  return (
    <section className="bg-surface2 p-3 min-w-0 space-y-1">
      <h3 className="text-xs text-text-dim mb-2">{title}</h3>
      {items.map(item => {
        const expanded = open.has(item.key);
        return (
          <div key={item.key}>
            <div className="flex items-center gap-1 min-w-0">
              <IconButton size="sm" onClick={() => toggle(item.key)} aria-label={t('tokenUsageExpand')}>
                {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              </IconButton>
              <div className="flex-1 min-w-0">
                <Row label={labelOf(item.key)} value={item.value} share={total ? item.value / total : 0} color={colorOf?.(item.key)} onPick={() => onPick(item.key)} />
              </div>
            </div>
            {expanded && split(item.key).map(sub => (
              <Row key={sub.key} indent label={splitLabelOf(sub.key)} value={sub.value} share={item.value ? sub.value / item.value : 0} onPick={() => onPickSplit(sub.key)} />
            ))}
          </div>
        );
      })}
      {note && <p className="text-xs text-text-dim pt-2">{note}</p>}
    </section>
  );
}

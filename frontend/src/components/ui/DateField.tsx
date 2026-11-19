/** ISO date field — a `YYYY-MM-DD` text field plus a Monday-first month grid in a Popover.
 *
 * Shows and accepts ISO dates only, whatever the browser locale (a native
 * `<input type=date>` renders mm/dd/yyyy and starts weeks on Sunday). Dates are
 * calendar days in UTC. Typed text is committed through `onChange` only when it is
 * a real date inside `min`..`max`; anything else turns the field red and commits
 * nothing, so the caller never acts on a half-typed value. Grid days outside
 * `min`..`max` are disabled; today is outlined.
 */

import { useState, type ChangeEvent } from 'react';
import { CalendarDays, ChevronLeft, ChevronRight } from 'lucide-react';
import { FieldInput } from './FieldInput';
import { IconButton } from './IconButton';
import { Popover } from './Popover';
import { useTranslation } from '../../i18n';

interface DateFieldProps {
  value: string;
  onChange: (value: string) => void;
  min?: string;
  max?: string;
  ariaLabel: string;
  /** Layout only (width / flex / margin), as for FieldInput. */
  className?: string;
}

const DAY_MS = 86_400_000;
const ISO_RE = /^\d{4}-\d{2}-\d{2}$/;
// 2024-01-01 is a Monday: the grid's first column.
const MONDAY_MS = Date.UTC(2024, 0, 1);

function iso(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10);
}

function isRealDate(s: string): boolean {
  return ISO_RE.test(s) && !Number.isNaN(Date.parse(`${s}T00:00:00Z`)) && iso(Date.parse(`${s}T00:00:00Z`)) === s;
}

function inRange(s: string, min?: string, max?: string): boolean {
  return (!min || s >= min) && (!max || s <= max);
}

function monthStart(s: string): number {
  const d = new Date(`${s}T00:00:00Z`);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1);
}

function shiftMonth(ms: number, by: number): number {
  const d = new Date(ms);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + by, 1);
}

/** 42 day timestamps: six Monday-first weeks covering the month of `first`. */
function gridDays(first: number): number[] {
  const lead = (new Date(first).getUTCDay() + 6) % 7;
  return Array.from({ length: 42 }, (_, i) => first + (i - lead) * DAY_MS);
}

export function DateField({ value, onChange, min, max, ariaLabel, className }: DateFieldProps) {
  const { t, language } = useTranslation();
  const [text, setText] = useState(value);
  const [shownValue, setShownValue] = useState(value);
  const [open, setOpen] = useState(false);
  const [view, setView] = useState(() => monthStart(isRealDate(value) ? value : iso(Date.now())));

  // WHY: adopt an external value change (a preset) during render, not in an effect —
  // an effect would paint one frame of the old text under the new range.
  if (value !== shownValue) {
    setShownValue(value);
    setText(value);
  }

  const valid = isRealDate(text) && inRange(text, min, max);

  const onText = (e: ChangeEvent<HTMLInputElement>) => {
    const next = e.target.value.trim();
    setText(next);
    if (isRealDate(next) && inRange(next, min, max) && next !== value) onChange(next);
  };

  const onOpenChange = (v: boolean) => {
    if (v) setView(monthStart(isRealDate(value) ? value : iso(Date.now())));
    setOpen(v);
  };

  const today = iso(Date.now());
  const weekdayFmt = new Intl.DateTimeFormat(language, { weekday: 'short', timeZone: 'UTC' });
  const monthFmt = new Intl.DateTimeFormat(language, { month: 'long', year: 'numeric', timeZone: 'UTC' });
  const viewMonth = new Date(view).getUTCMonth();

  return (
    <div className={`flex items-center ${className ?? ''}`.trim()}>
      <FieldInput
        value={text}
        onChange={onText}
        invalid={!valid}
        placeholder="YYYY-MM-DD"
        maxLength={10}
        inputMode="numeric"
        aria-label={ariaLabel}
        className="w-[7.5rem] tabular-nums"
      />
      <Popover
        open={open}
        onOpenChange={onOpenChange}
        keyboardNav={false}
        align="right"
        panelClassName="p-2 border border-border"
        trigger={<IconButton aria-label={ariaLabel}><CalendarDays size={14} /></IconButton>}
      >
        {({ close }) => (
          <div className="w-[15rem]">
            <div className="flex items-center justify-between mb-1">
              <IconButton onClick={() => setView(v => shiftMonth(v, -1))} aria-label={t('previous')}><ChevronLeft size={14} /></IconButton>
              <span className="text-ui-sm text-text">{monthFmt.format(view)}</span>
              <IconButton onClick={() => setView(v => shiftMonth(v, 1))} aria-label={t('next')}><ChevronRight size={14} /></IconButton>
            </div>
            <div className="grid grid-cols-7">
              {Array.from({ length: 7 }, (_, i) => (
                <span key={i} className="h-[24px] flex items-center justify-center text-ui-2xs text-text-dim">
                  {weekdayFmt.format(MONDAY_MS + i * DAY_MS)}
                </span>
              ))}
              {gridDays(view).map(ms => {
                const day = iso(ms);
                const selected = day === value;
                const enabled = inRange(day, min, max);
                const outside = new Date(ms).getUTCMonth() !== viewMonth;
                const tone = selected
                  ? 'bg-accent text-white'
                  : `bg-transparent ${outside ? 'text-text-dim' : 'text-text'} hover:bg-surface3`;
                const mark = day === today && !selected ? ' outline outline-1 -outline-offset-1 outline-accent' : '';
                return (
                  <button
                    key={day}
                    type="button"
                    disabled={!enabled}
                    aria-pressed={selected}
                    aria-label={day}
                    onClick={() => { onChange(day); close(); }}
                    className={`h-[30px] text-ui-xs tabular-nums border-none cursor-pointer ${tone}${mark} disabled:opacity-30 disabled:cursor-default disabled:hover:bg-transparent`}
                  >
                    {new Date(ms).getUTCDate()}
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </Popover>
    </div>
  );
}

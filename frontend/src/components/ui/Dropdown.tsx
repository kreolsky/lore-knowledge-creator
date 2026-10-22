/** SYSTEM: ui-primitives — value-selector built on Popover.
 *
 * Renders a trigger button (label + chevron) with the darker-than-field
 * background (CSS var `--composer-control-bg`, darker than the composer field
 * in BOTH themes), active-row highlight, keyboard nav seeded to the current
 * value, and optional Agent-active orange trigger (`highlight`).
 *
 * ARCH: roles — trigger is a plain Button; the panel list uses `role="listbox"`
 * with `role="option"` rows (preserved from the legacy ModeDropdown). Disabled
 * options are skipped by arrow nav (Popover's isItemDisabled) and re-guarded in
 * onSelect. INVARIANT: no rounded corners; trigger bg via --composer-control-bg.  Why: project rule (border-radius: 0 everywhere); the trigger bg uses --composer-control-bg for theme consistency.
 */

import { useMemo, useCallback, useEffect, useRef, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { Popover } from './Popover';
import { Button } from './Button';
import { FieldInput } from './FieldInput';

export interface DropdownOption {
  value: string;
  label: string;
  disabled?: boolean;
  note?: string;
  // ARCH: optional trailing badge rendered inside the option row AND on the
  // trigger (options[selectedIndex]?.badge). Strictly additive: callers that don't
  // pass it are unaffected. Used for the no-vision EyeOff mark (see SYSTEM:
  // ui-primitives above).
  badge?: React.ReactNode;
}

export interface DropdownProps {
  value: string;
  options: DropdownOption[];
  onSelect: (value: string) => void;
  triggerLabel?: string;
  icon?: React.ReactNode;
  title?: string;
  disabled?: boolean;
  highlight?: boolean;
  variant?: 'composer' | 'highlight' | 'ghost' | 'panel';
  align?: 'left' | 'right';
  placement?: 'top' | 'bottom';
  /** Trigger height: `sm` (compact rows), `md` = the md Button's height, for a
   * selector sharing a row with md buttons (chat composer: send), or `lg` =
   * FieldInput's 37px, for a selector sitting in the same row as inputs (admin forms). */
  size?: 'sm' | 'md' | 'lg';
  /** Controlled open state, passed through to Popover; omit for uncontrolled. */
  open?: boolean;
  onOpenChange?: (v: boolean) => void;
  /** Set ⇒ the open panel starts with a filter field (this placeholder) that
   * narrows the options by label or note; the query resets on close. */
  filterPlaceholder?: string;
  /** Trigger fills the row's free width (flex-1) instead of sizing to its label. */
  fullWidth?: boolean;
}

export function Dropdown({
  value,
  options,
  onSelect,
  triggerLabel,
  icon,
  title,
  disabled = false,
  highlight = false,
  variant,
  align = 'left',
  placement = 'top',
  size = 'sm',
  open,
  onOpenChange,
  filterPlaceholder,
  fullWidth = false,
}: DropdownProps) {
  const [innerOpen, setInnerOpen] = useState(false);
  const [query, setQuery] = useState('');
  const isOpen = open ?? innerOpen;
  const filterRef = useRef<HTMLInputElement>(null);
  useEffect(() => { if (isOpen) filterRef.current?.focus(); }, [isOpen]);
  const setOpen = useCallback((v: boolean) => {
    if (!v) setQuery('');
    if (open === undefined) setInnerOpen(v);
    onOpenChange?.(v);
  }, [open, onOpenChange]);

  // The panel lists `shown`; keyboard nav, Enter and the cursor seed all index
  // into it, so a filtered list navigates exactly like an unfiltered one.
  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (filterPlaceholder === undefined || !needle) return options;
    return options.filter(o =>
      o.label.toLowerCase().includes(needle) || !!o.note?.toLowerCase().includes(needle));
  }, [options, query, filterPlaceholder]);

  // -1 when no option carries `value` (an action picker such as "Add user…"):
  // the keyboard cursor then seeds on nothing, so no row opens pre-highlighted
  // — ArrowDown from -1 lands on 0, Enter on -1 is a no-op.
  const selectedIndex = useMemo(
    () => shown.findIndex(o => o.value === value),
    [shown, value],
  );

  // The trigger still falls back to the first option's label/badge (callers
  // whose value lands a render later, e.g. an auto-picked move target).
  const triggerIndex = Math.max(options.findIndex(o => o.value === value), 0);
  const label = triggerLabel ?? options[triggerIndex]?.label ?? '';

  const isItemDisabled = useCallback((i: number) => !!shown[i]?.disabled, [shown]);

  const handleEnter = useCallback((i: number) => {
    const opt = shown[i];
    if (opt && !opt.disabled) onSelect(opt.value);
  }, [shown, onSelect]);

  return (
    <Popover
      trigger={
        <Button
          // WHY: a button inside a <form> defaults to type=submit — opening a
          // selector must never submit the form around it (admin add-user form).
          type="button"
          variant={variant ?? (highlight ? 'highlight' : 'composer')}
          size={size}
          title={title}
          disabled={disabled}
          fullWidth={fullWidth}
        >
          {icon && <span className="mr-1 shrink-0">{icon}</span>}
          <span className={`truncate ${fullWidth ? 'min-w-0' : 'max-w-[180px]'}`}>{label}</span>
          {options[triggerIndex]?.badge && (
            <span className="shrink-0">{options[triggerIndex].badge}</span>
          )}
          <ChevronDown size={12} className="ml-1 opacity-60 shrink-0" />
        </Button>
      }
      align={align}
      placement={placement}
      open={isOpen}
      onOpenChange={setOpen}
      rootClassName={fullWidth ? 'flex-1 min-w-0' : ''}
      keyboardNav
      itemCount={shown.length}
      isItemDisabled={isItemDisabled}
      onEnter={handleEnter}
      seedIndex={selectedIndex}
      disabled={disabled}
      panelClassName="list-scroll w-max min-w-full"
    >
      {({ activeIndex, close }) => (
        <div role="listbox">
          {filterPlaceholder !== undefined && (
            <div className="p-1.5">
              <FieldInput
                ref={filterRef}
                value={query}
                placeholder={filterPlaceholder}
                onChange={e => setQuery(e.target.value)}
              />
            </div>
          )}
          {shown.map((opt, i) => {
            const isActive = opt.value === value;
            const isCursor = i === activeIndex;
            // ARCH: `truncate` lives on the LABEL span, not the row. The row wraps
            // label + badge in an inline-flex span (min-w-0 lets the label ellipsize
            // while the shrink-0 badge stays full-size). The row stays `block` so the
            // `note` block keeps stacking below — widening `label` would break that.
            const base = 'block w-full text-left px-3 py-1.5 text-ui-xs';
            let cls: string;
            if (isActive) {
              cls = `${base} bg-surface2 ${opt.disabled ? 'text-text-dim cursor-not-allowed' : 'text-text cursor-pointer'}`;
            } else if (isCursor) {
              cls = `${base} bg-accent-soft ${opt.disabled ? 'text-accent-text/40 cursor-not-allowed' : 'text-accent-text cursor-pointer'}`;
            } else {
              cls = `${base} ${opt.disabled ? 'text-text-dim cursor-not-allowed' : 'text-text-muted hover:bg-surface2 cursor-pointer'}`;
            }
            return (
              <div
                key={opt.value}
                role="option"
                aria-selected={isActive}
                aria-disabled={opt.disabled}
                title={opt.note}
                onClick={() => {
                  if (opt.disabled) return;
                  onSelect(opt.value);
                  close();
                }}
                className={cls}
              >
                <span className="inline-flex items-center gap-1 min-w-0 max-w-full">
                  <span className="truncate">{opt.label}</span>
                  {opt.badge && <span className="shrink-0">{opt.badge}</span>}
                </span>
                {opt.note && (
                  <span className="block text-text-dim text-ui-2xs mt-0.5 truncate normal-case">
                    {opt.note}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </Popover>
  );
}

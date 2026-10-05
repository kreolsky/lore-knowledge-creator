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

import { useMemo, useCallback } from 'react';
import { ChevronDown } from 'lucide-react';
import { Popover } from './Popover';
import { Button } from './Button';

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
  variant?: 'composer' | 'highlight' | 'ghost';
  align?: 'left' | 'right';
  placement?: 'top' | 'bottom';
  /** Trigger height: `sm` (composer rows) or `lg` = FieldInput's 37px, for a
   * selector sitting in the same row as inputs (admin forms). */
  size?: 'sm' | 'lg';
  /** Controlled open state, passed through to Popover; omit for uncontrolled. */
  open?: boolean;
  onOpenChange?: (v: boolean) => void;
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
}: DropdownProps) {
  // -1 when no option carries `value` (an action picker such as "Add user…"):
  // the keyboard cursor then seeds on nothing, so no row opens pre-highlighted
  // — ArrowDown from -1 lands on 0, Enter on -1 is a no-op.
  const selectedIndex = useMemo(
    () => options.findIndex(o => o.value === value),
    [options, value],
  );

  // The trigger still falls back to the first option's label/badge (callers
  // whose value lands a render later, e.g. an auto-picked move target).
  const labelIndex = selectedIndex >= 0 ? selectedIndex : 0;
  const label = triggerLabel ?? options[labelIndex]?.label ?? '';

  const isItemDisabled = useCallback((i: number) => !!options[i]?.disabled, [options]);

  const handleEnter = useCallback((i: number) => {
    const opt = options[i];
    if (opt && !opt.disabled) onSelect(opt.value);
  }, [options, onSelect]);

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
        >
          {icon && <span className="mr-1 shrink-0">{icon}</span>}
          <span className="truncate max-w-[180px]">{label}</span>
          {options[labelIndex]?.badge && (
            <span className="shrink-0">{options[labelIndex].badge}</span>
          )}
          <ChevronDown size={12} className="ml-1 opacity-60 shrink-0" />
        </Button>
      }
      align={align}
      placement={placement}
      open={open}
      onOpenChange={onOpenChange}
      keyboardNav
      itemCount={options.length}
      isItemDisabled={isItemDisabled}
      onEnter={handleEnter}
      seedIndex={selectedIndex}
      disabled={disabled}
      panelClassName="list-scroll w-max min-w-full"
    >
      {({ activeIndex, close }) => (
        <div role="listbox">
          {options.map((opt, i) => {
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
              cls = `${base} bg-accent-soft ${opt.disabled ? 'text-accent/40 cursor-not-allowed' : 'text-accent cursor-pointer'}`;
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

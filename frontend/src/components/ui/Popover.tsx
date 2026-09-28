/** SYSTEM: ui-primitives — shared popup shell.
 *
 * Owns the boilerplate duplicated across the composer selectors, ChatHeader
 * dropdowns, and DownloadMenu: open state (uncontrolled by default; optional
 * controlled `open`/`onOpenChange`), outside-click close (via useClickOutside),
 * the absolutely-positioned panel with the single shared class
 * `bg-surface border border-border shadow-lg z-50`, alignment/placement, and
 * optional ↑/↓/Enter/Esc keyboard navigation.
 *
 * ARCH: generic keyboard nav is parameterized by `itemCount` + `isItemDisabled`
 * + `onEnter` so value-selectors (Dropdown) and menus (ChatHeader sessions) get
 * consistent Arrow/Enter/Esc behavior without re-implementing it. Menus that
 * embed native anchors or busy items (DownloadMenu) set `keyboardNav={false}`.
 * The render-prop children receive `{ close, activeIndex }` so
 * rows can highlight the keyboard cursor and call `close()` after a selection.
 *
 * ARCH: the trigger is cloned (NOT wrapped) so it stays a DIRECT child of the
 * root div — preserves direct-child CSS like `.download-menu > button`.
 * INVARIANT: no rounded corners (border-radius: 0 project rule). Consumers keep
 * their own ARIA roles (listbox/option inside Dropdown; button/menuitem in
 * menus) — Popover renders a plain presentation div to avoid role conflicts.  Why: border-radius 0 (project rule); consumers own their ARIA roles, so Popover renders a plain presentation div to avoid role conflicts.
 */

import {
  useState,
  useRef,
  useCallback,
  useEffect,
  cloneElement,
  isValidElement,
  type ReactNode,
  type MouseEvent,
} from 'react';
import { useClickOutside } from '../../hooks/useClickOutside';

export interface PopoverCtx {
  close: () => void;
  activeIndex: number;
}

export interface PopoverProps {
  trigger: ReactNode;
  children: (ctx: PopoverCtx) => ReactNode;
  open?: boolean;
  onOpenChange?: (v: boolean) => void;
  align?: 'left' | 'right';
  placement?: 'top' | 'bottom';
  panelClassName?: string;
  rootClassName?: string;
  rootDataTheme?: string;
  keyboardNav?: boolean;
  closeOnSelect?: boolean;
  disabled?: boolean;
  /** Item count for ↑/↓ + Enter bounds. Omit/0 ⇒ arrow/enter disabled. */
  itemCount?: number;
  /** Skip disabled rows while navigating. */
  isItemDisabled?: (i: number) => boolean;
  /** Invoked on Enter with the active index (only when that row is enabled). */
  onEnter?: (i: number) => void;
  /** Initial activeIndex when the panel opens (keyboard cursor). */
  seedIndex?: number;
}

const PANEL_BASE = 'absolute bg-surface shadow-lg z-50';

export function Popover({
  trigger,
  children,
  open: controlledOpen,
  onOpenChange,
  align = 'left',
  placement = 'bottom',
  panelClassName = '',
  rootClassName = '',
  rootDataTheme,
  keyboardNav = true,
  closeOnSelect = true,
  disabled = false,
  itemCount = 0,
  isItemDisabled,
  onEnter,
  seedIndex = 0,
}: PopoverProps) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const isControlled = controlledOpen !== undefined;
  const open = isControlled ? controlledOpen! : uncontrolledOpen;

  const setOpen = useCallback((v: boolean) => {
    if (isControlled) onOpenChange?.(v);
    else setUncontrolledOpen(v);
  }, [isControlled, onOpenChange]);

  const rootRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  const close = useCallback(() => setOpen(false), [setOpen]);

  // ARCH: pass the stable `close` callback (not an inline arrow) so the
  // useClickOutside effect doesn't tear down + re-attach the mousedown listener
  // on every re-render while open (e.g. each Arrow key press bumps activeIndex).
  useClickOutside(rootRef, close, open);

  const [activeIndex, setActiveIndexState] = useState(0);

  const toggle = useCallback(() => {
    if (disabled) return;
    setOpen(!open);
  }, [disabled, open, setOpen]);

  // Focus the panel on open so it captures keydown, and seed the keyboard
  // cursor (e.g. Dropdown seeds it to the currently-selected value).
  useEffect(() => {
    if (!open) return;
    setActiveIndexState(seedIndex);
    const id = requestAnimationFrame(() => panelRef.current?.focus());
    return () => cancelAnimationFrame(id);
  }, [open, seedIndex]);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      setOpen(false);
      return;
    }
    if (!keyboardNav || itemCount === 0) return;
    const disabledAt = (i: number) => isItemDisabled?.(i) ?? false;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIndexState(prev => {
        let i = prev;
        for (let n = 0; n < itemCount; n++) {
          i = (i + 1) % itemCount;
          if (!disabledAt(i)) return i;
        }
        return prev;
      });
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIndexState(prev => {
        let i = prev;
        for (let n = 0; n < itemCount; n++) {
          i = (i - 1 + itemCount) % itemCount;
          if (!disabledAt(i)) return i;
        }
        return prev;
      });
    } else if (e.key === 'Enter') {
      if (disabledAt(activeIndex)) return;
      e.preventDefault();
      onEnter?.(activeIndex);
      if (closeOnSelect) setOpen(false);
    }
  }, [keyboardNav, itemCount, isItemDisabled, onEnter, closeOnSelect, setOpen, activeIndex]);

  const triggerWithToggle = isValidElement(trigger)
    ? cloneElement(trigger as React.ReactElement<{ onClick?: (e: MouseEvent) => void }>, {
        onClick: (e: MouseEvent) => {
          const own = (trigger as React.ReactElement<{ onClick?: (e: MouseEvent) => void }>).props.onClick;
          own?.(e);
          if (e.defaultPrevented) return;
          toggle();
        },
      })
    : trigger;

  const alignClass = align === 'right' ? 'right-0' : 'left-0';
  const placementClass = placement === 'top' ? 'bottom-full mb-1' : 'top-full mt-1';

  return (
    <div
      className={`relative ${rootClassName}`.trim()}
      data-theme={rootDataTheme}
      ref={rootRef}
    >
      {triggerWithToggle}
      {open && (
        <div
          ref={panelRef}
          role="presentation"
          tabIndex={-1}
          onKeyDown={handleKeyDown}
          className={`${PANEL_BASE} ${alignClass} ${placementClass} ${panelClassName}`.trim()}
        >
          {children({ close, activeIndex })}
        </div>
      )}
    </div>
  );
}

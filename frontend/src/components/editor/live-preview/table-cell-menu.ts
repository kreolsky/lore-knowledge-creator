/**
 * Table cell context menu — a plain-DOM menu opened at the pointer by a right-click on a
 * cell of an editable table (see SYSTEM: table-block in `table-block-widget.ts`, which owns
 * the actions; this file only builds, places and closes the DOM). Pointer only.
 */

// WHY inline SVG: this menu is plain DOM, and lucide has no glyph that tells "above" from
// "below". Own drawings on lucide's 24-grid and stroke: the affected rows/columns as square
// bars (no rounded corners) plus a chevron/arrow/cross on the side the action lands.
const ICON_PATHS = {
  insertRowAbove: '<path d="m9 6 3-3 3 3"/><rect width="18" height="4" x="3" y="10"/><rect width="18" height="4" x="3" y="17"/>',
  insertRowBelow: '<rect width="18" height="4" x="3" y="3"/><rect width="18" height="4" x="3" y="10"/><path d="m9 18 3 3 3-3"/>',
  insertColumnLeft: '<path d="m6 9-3 3 3 3"/><rect width="4" height="18" x="10" y="3"/><rect width="4" height="18" x="17" y="3"/>',
  insertColumnRight: '<rect width="4" height="18" x="3" y="3"/><rect width="4" height="18" x="10" y="3"/><path d="m18 9 3 3-3 3"/>',
  moveRowUp: '<path d="M12 12V3"/><path d="m8 7 4-4 4 4"/><rect width="18" height="4" x="3" y="17"/>',
  moveRowDown: '<rect width="18" height="4" x="3" y="3"/><path d="M12 12v9"/><path d="m8 17 4 4 4-4"/>',
  moveColumnLeft: '<path d="M12 12H3"/><path d="m7 8-4 4 4 4"/><rect width="4" height="18" x="17" y="3"/>',
  moveColumnRight: '<rect width="4" height="18" x="3" y="3"/><path d="M12 12h9"/><path d="m17 8 4 4-4 4"/>',
  deleteRow: '<rect width="18" height="4" x="3" y="3"/><path d="m9 13 6 6"/><path d="m15 13-6 6"/>',
  deleteColumn: '<rect width="4" height="18" x="3" y="3"/><path d="m13 9 6 6"/><path d="m19 9-6 6"/>',
  mergeCells: '<rect width="18" height="18" x="3" y="3"/><path d="m7 12 3-3"/><path d="m7 12 3 3"/><path d="M7 12h10"/><path d="m17 12-3-3"/><path d="m17 12-3 3"/>',
  unmergeCells: '<rect width="18" height="18" x="3" y="3"/><path d="M12 3v18"/><path d="m5 12 3-3"/><path d="m5 12 3 3"/><path d="m19 12-3-3"/><path d="m19 12-3 3"/>',
  deleteTable: '<path d="M3 6h18"/><path d="M5 6v15h14V6"/><path d="M9 6V3h6v3"/><path d="M10 11v6"/><path d="M14 11v6"/>',
} as const;

export type CellMenuIcon = keyof typeof ICON_PATHS;

function iconSvg(icon: CellMenuIcon): string {
  return '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24"'
    + ' fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"'
    + ` stroke-linejoin="round" aria-hidden="true" class="shrink-0">${ICON_PATHS[icon]}</svg>`;
}

export interface CellMenuItem {
  label: string;
  icon: CellMenuIcon;
  run: () => void;
  disabled?: boolean;
  danger?: boolean;
}

export type CellMenuEntry = CellMenuItem | 'separator';

const ITEM_BASE = 'flex items-center gap-2 w-full text-left px-3 py-1.5 text-ui-xs whitespace-nowrap';
const ITEM_IDLE = `${ITEM_BASE} text-text-muted hover:bg-surface2 cursor-pointer`;
const ITEM_DANGER = `${ITEM_BASE} text-red hover:bg-surface2 cursor-pointer`;
const ITEM_DISABLED = `${ITEM_BASE} text-text-dim cursor-not-allowed`;

/** The one open menu's closer — a second right-click replaces the first menu. */
let closeOpen: (() => void) | null = null;

/** Close the open menu, if any. */
export function closeCellMenu(): void {
  closeOpen?.();
}

/**
 * Open the menu at viewport point (x, y), clamped inside the viewport. Closes on Esc, a
 * press outside it, any scroll, or after an item runs. Returns the closer (idempotent).
 * WHY mousedown preventDefault on the menu: the clicked cell keeps focus, so an insert's
 * focus move starts from it and a cancelled menu leaves the caret where it was.
 */
export function openCellMenu(x: number, y: number, entries: readonly CellMenuEntry[]): () => void {
  closeOpen?.();
  const menu = document.createElement('div');
  menu.className = 'cm-table-cell-menu fixed z-50 py-1 bg-surface border border-border shadow-lg';
  menu.setAttribute('role', 'menu');
  menu.addEventListener('mousedown', (e) => { e.preventDefault(); e.stopPropagation(); });
  menu.addEventListener('contextmenu', (e) => e.preventDefault());

  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    menu.remove();
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('pointerdown', onOutside, true);
    window.removeEventListener('scroll', close, true);
    window.removeEventListener('blur', close);
    if (closeOpen === close) closeOpen = null;
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key !== 'Escape') return;
    e.preventDefault();
    e.stopPropagation();
    close();
  };
  const onOutside = (e: PointerEvent) => { if (!menu.contains(e.target as Node)) close(); };

  for (const entry of entries) {
    if (entry === 'separator') {
      const sep = document.createElement('div');
      sep.className = 'my-1 border-t border-border';
      sep.setAttribute('role', 'separator');
      menu.appendChild(sep);
      continue;
    }
    const item = document.createElement('div');
    item.setAttribute('role', 'menuitem');
    item.innerHTML = iconSvg(entry.icon);
    item.append(entry.label);
    item.className = entry.disabled ? ITEM_DISABLED : entry.danger ? ITEM_DANGER : ITEM_IDLE;
    if (entry.disabled) item.setAttribute('aria-disabled', 'true');
    item.addEventListener('click', () => {
      if (entry.disabled) return;
      close();
      entry.run();
    });
    menu.appendChild(item);
  }

  document.body.appendChild(menu);
  const rect = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(0, Math.min(x, window.innerWidth - rect.width))}px`;
  menu.style.top = `${Math.max(0, Math.min(y, window.innerHeight - rect.height))}px`;
  document.addEventListener('keydown', onKey, true);
  document.addEventListener('pointerdown', onOutside, true);
  window.addEventListener('scroll', close, true);
  window.addEventListener('blur', close);
  closeOpen = close;
  return close;
}

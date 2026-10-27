/**
 * Table rendering widget — replaces raw markdown table with an HTML <table>.
 * WHY cells go through `renderCellStatic`: a hand-written inline renderer here knew no links,
 * so `[text](url)` and internal links showed raw until the cursor entered the table; the
 * cell pipeline renders exactly as the editor does.
 */
import { WidgetType, type EditorView } from '@codemirror/view';
import { renderCellStatic } from './cell-static-render';
import { createBoundedMemo } from '../../../utils/bounded-memo';

// INVARIANT: a rendered table's measured height is cached by rawText and reused
// as estimatedHeight, so an OFF-SCREEN table keeps its real height in CM6's
// height model instead of collapsing to one line.  Why: the measured height is cached by rawText and reused as estimatedHeight so an off-screen table keeps its real height in CM6's height model instead of collapsing to one line.
// Why: a block widget that returns -1 (no estimate) is sized at defaultLineHeight
// while off-screen, but renders ~10-20× taller. As tables enter/leave the rendered
// window during scroll, the document's total height swings by hundreds of px and
// CM6 corrects scrollTop — a large upward jerk, worst near doc end where the
// inflated scrollTop exceeds the re-estimated height and gets clamped.
// INVARIANT: cached/estimated heights are rounded to whole pixels.
// Why: a fractional block-widget height makes CM6's BlockGapWidget rounding
// fluctuate getBoundingClientRect across measure ticks, restarting the measure
// loop ("Measure loop restarted more than 5 times") which then breaks BEFORE
// scroll-anchor compensation runs — the visible jump. See codemirror/dev#1341.
// See incident 2026-05-30 (nested-list / table scroll jump).
// WHY bounded LRU (was: insertion-order FIFO at 200): heights are RE-READ per
// scroll (estimatedHeight), so recency is the right bound — a table that keeps
// being estimated stays cached; the old FIFO evicted it once enough NEW tables
// rendered. Policy and measurement live in utils/bounded-memo.ts (plan
// resource-cache-one-primitive, step 6).
const tableHeightCache = createBoundedMemo<number>(200);
// Fallback for a table never yet rendered (nothing cached): row-based estimate.
// MUST NOT be -1/0 — a too-small first estimate reintroduces the scroll jerk on
// the FIRST scroll-through, before any measurement exists. ~38px/row ≈ 26px
// line-height + 8px cell padding + 1px border; refined to the exact measured
// height on first render.
const ESTIMATED_ROW_HEIGHT = 38;
const TABLE_WRAP_PADDING = 8;

export class TableWidget extends WidgetType {
  constructor(readonly rawText: string) { super(); }

  toDOM(): HTMLElement {
    // INVARIANT: outer wrapper carries vertical spacing as padding, NOT margin.
    // Why: CM6 measures block widget height via getBoundingClientRect, which excludes
    // margin. A margin here lost ~8px from the model and shifted posAtCoords below the
    // table by roughly a third of a line — clicks landed one line lower than intended.
    const wrap = document.createElement('div');
    wrap.className = 'cm-table-widget-wrap';

    // Rows keep their index in rawText: data-row is the line offset from the table's first line.
    const lines = this.rawText.split('\n').map((text, row) => ({ text, row })).filter((l) => l.text.trim());
    // Each cell carries `off`, its text start within the RAW row line (indent and leading `|`
    // counted), so a click maps back to the source.
    const parseRow = (line: string): { text: string; off: number }[] => {
      const base = /^\s*\|?/.exec(line)![0].length;
      const stripped = line.slice(base).replace(/\|\s*$/, '');
      const cells: { text: string; off: number }[] = [];
      let cell = '';
      let cellStart = 0;
      let inCode = false;
      const push = () => cells.push({
        text: cell.trim(),
        off: base + cellStart + (cell.length - cell.trimStart().length),
      });

      for (let i = 0; i < stripped.length; i++) {
        const ch = stripped[i];
        if (ch === '`') {
          inCode = !inCode;
          cell += ch;
        } else if (ch === '|' && !inCode) {
          push();
          cell = '';
          cellStart = i + 1;
        } else {
          cell += ch;
        }
      }

      if (cell.trim()) push();
      return cells;
    };

    const renderCell = (tag: 'th' | 'td', cell: { text: string; off: number }, row: number) => {
      const el = document.createElement(tag);
      el.dataset.row = String(row);
      el.dataset.colOff = String(cell.off);
      el.appendChild(renderCellStatic(cell.text, { stampOffsets: true }));
      return el;
    };

    const table = document.createElement('table');
    table.className = 'cm-table-widget';

    if (lines.length > 0) {
      const thead = document.createElement('thead');
      const tr = document.createElement('tr');
      for (const cell of parseRow(lines[0].text)) tr.appendChild(renderCell('th', cell, lines[0].row));
      thead.appendChild(tr);
      table.appendChild(thead);
    }

    if (lines.length > 2) {
      const tbody = document.createElement('tbody');
      for (let i = 2; i < lines.length; i++) {
        const tr = document.createElement('tr');
        for (const cell of parseRow(lines[i].text)) tr.appendChild(renderCell('td', cell, lines[i].row));
        tbody.appendChild(tr);
      }
      table.appendChild(tbody);
    }

    wrap.appendChild(table);
    // Measure after layout and cache the real height so subsequent off-screen
    // estimates are exact (correct posAtCoords + zero scroll swing on re-scroll).
    requestAnimationFrame(() => {
      const measured = Math.round(wrap.getBoundingClientRect().height);
      if (measured > 0) {
        tableHeightCache.set(this.rawText, measured);
      }
    });
    return wrap;
  }

  eq(other: TableWidget): boolean { return this.rawText === other.rawText; }
  ignoreEvent(): boolean { return false; }

  get estimatedHeight(): number {
    const cached = tableHeightCache.get(this.rawText);
    if (cached != null) return cached;
    const rows = this.rawText.split('\n').filter((l) => l.trim()).length;
    return rows * ESTIMATED_ROW_HEIGHT + TABLE_WRAP_PADDING;
  }
}

/** Offset inside a plain-text cell (a bare text node) under the pointer, else null. */
function caretOffsetIn(textNode: Node, x: number, y: number): number | null {
  const doc = document as Document & {
    caretPositionFromPoint?: (x: number, y: number) => { offsetNode: Node; offset: number } | null;
  };
  if (doc.caretPositionFromPoint) {
    const p = doc.caretPositionFromPoint(x, y);
    return p && p.offsetNode === textNode ? p.offset : null;
  }
  const r = document.caretRangeFromPoint?.(x, y);
  return r && r.startContainer === textNode ? r.startOffset : null;
}

/**
 * Absolute doc position a mouse event inside a rendered GFM table points at, or null when
 * the target is not a table cell. Exact char for a plain-text cell, start of the clicked
 * rendered element otherwise.
 */
export function widgetClickPos(view: EditorView, event: MouseEvent): number | null {
  const target = event.target instanceof Element ? event.target : (event.target as Node | null)?.parentElement;
  const cell = target?.closest<HTMLElement>('th[data-row], td[data-row]');
  const wrap = cell?.closest('.cm-table-widget-wrap');
  if (!cell || !wrap || !view.contentDOM.contains(wrap)) return null;
  const { doc } = view.state;
  const first = doc.lineAt(view.posAtDOM(wrap));
  const row = Number(cell.dataset.row);
  if (first.number + row > doc.lines) return null;
  // WHY the indent is read here, not stamped: rawText (the eq() key) starts after row 0's
  // indent, so a reused widget DOM may sit under a different indent. Row 0's offsets are
  // relative to the table node, later rows' to their line start.
  const indent = row === 0 ? /^\s*/.exec(first.text)![0].length : 0;
  const plain = cell.childNodes.length === 1 && cell.firstChild instanceof Text ? cell.firstChild : null;
  const inCell = plain
    ? caretOffsetIn(plain, event.clientX, event.clientY) ?? 0
    : Number(target!.closest<HTMLElement>('[data-src-off]')?.dataset.srcOff ?? 0);
  const line = doc.line(first.number + row);
  return Math.min(line.from + indent + Number(cell.dataset.colOff) + inCell, line.to);
}

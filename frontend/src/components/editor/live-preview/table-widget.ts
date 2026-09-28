/**
 * Table rendering widget — replaces raw markdown table with an HTML <table>.
 */
import { WidgetType } from '@codemirror/view';
import { renderInlineMarkdown } from './inline-markdown';
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

    const lines = this.rawText.split('\n').filter((l) => l.trim());
    const parseRow = (line: string): string[] => {
      const stripped = line.replace(/^\|/, '').replace(/\|$/, '');
      const cells: string[] = [];
      let cell = '';
      let inCode = false;

      for (let i = 0; i < stripped.length; i++) {
        const ch = stripped[i];
        if (ch === '`') {
          inCode = !inCode;
          cell += ch;
        } else if (ch === '|' && !inCode) {
          cells.push(cell.trim());
          cell = '';
        } else {
          cell += ch;
        }
      }

      if (cell.trim()) cells.push(cell.trim());
      return cells;
    };

    const table = document.createElement('table');
    table.className = 'cm-table-widget';

    if (lines.length > 0) {
      const thead = document.createElement('thead');
      const tr = document.createElement('tr');
      for (const cell of parseRow(lines[0])) {
        const th = document.createElement('th');
        th.appendChild(renderInlineMarkdown(cell));
        tr.appendChild(th);
      }
      thead.appendChild(tr);
      table.appendChild(thead);
    }

    if (lines.length > 2) {
      const tbody = document.createElement('tbody');
      for (let i = 2; i < lines.length; i++) {
        const tr = document.createElement('tr');
        for (const cell of parseRow(lines[i])) {
          const td = document.createElement('td');
          td.appendChild(renderInlineMarkdown(cell));
          tr.appendChild(td);
        }
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

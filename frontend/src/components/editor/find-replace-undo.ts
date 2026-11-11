/**
 * Replace-all over host text + anchored table cells, and its one-shot panel Undo.
 *
 * ARCH: Replace-all writes the host `content` Y.Text and every matched cell Y.Text
 * DIRECTLY in one `ydoc.transact(fn, REPLACE_ALL_ORIGIN)` — ONE step on the entity's
 * document-wide undo stack (see collab/document-undo.ts), so Ctrl+Z in any editor and the
 * panel's Undo line revert the same single item, host + cells together.
 */

import { Text } from '@codemirror/state';
import type { SearchQuery } from '@codemirror/search';
import * as Y from 'yjs';
import { cellKey, getCellText, orderedColIds, orderedRowIds } from './live-preview/table-block-model';
import { TABLE_ANCHOR_RE } from './live-preview/table-document-ops';
import { documentUndo } from '../../collab/document-undo';

/** Transaction origin of a Replace-all write — tracked by the document-wide undo stack. */
export const REPLACE_ALL_ORIGIN = Symbol('find-replace-all');

export interface ReplaceEdit { from: number; to: number; insert: string }

// WHY mirrored: SearchQuery.unquote / getReplacement are @internal in
// @codemirror/search — same semantics, kept here so a cell replace and the stock
// host replace produce identical text.
function unquote(query: SearchQuery, text: string): string {
  return query.literal ? text
    : text.replace(/\\([nrt\\])/g, (_, ch: string) => (ch === 'n' ? '\n' : ch === 'r' ? '\r' : ch === 't' ? '\t' : '\\'));
}

function replacementFor(query: SearchQuery, match: RegExpExecArray | null): string {
  const base = unquote(query, query.replace);
  if (!query.regexp || !match) return base;
  return base.replace(/\$([$&]|\d+)/g, (m, i: string) => {
    if (i === '&') return match[0];
    if (i === '$') return '$';
    for (let l = i.length; l > 0; l--) {
      const n = +i.slice(0, l);
      if (n > 0 && n < match.length) return match[n] + i.slice(l);
    }
    return m;
  });
}

/** Every non-empty match of `query` in `text` as a replace edit, document order, uncapped. */
export function replaceEdits(query: SearchQuery, text: string): ReplaceEdit[] {
  if (!query.valid) return [];
  const out: ReplaceEdit[] = [];
  const cursor = query.getCursor(Text.of(text.split('\n')));
  for (;;) {
    const step = cursor.next();
    if (step.done) break;
    const v = step.value as { from: number; to: number; match?: RegExpExecArray };
    if (v.from === v.to) continue;
    out.push({ from: v.from, to: v.to, insert: replacementFor(query, v.match ?? null) });
  }
  return out;
}

export interface ReplaceAllPlan {
  host: ReplaceEdit[];
  /** Cell edits keyed `${tableId}\u0000${cellKey(rowId, colId)}`. */
  cells: Map<string, { text: Y.Text; edits: ReplaceEdit[] }>;
  cellCount: number;
}

/** Uncapped edit set over host text (anchor-crossing matches dropped) and every
 *  cell of every ANCHORED table (first occurrence of an id). */
export function planReplaceAll(query: SearchQuery, hostText: string, ydoc: Y.Doc): ReplaceAllPlan {
  const anchors: { id: string; from: number; to: number }[] = [];
  const seen = new Set<string>();
  for (const m of hostText.matchAll(TABLE_ANCHOR_RE)) {
    anchors.push({ id: m[2], from: m.index, to: m.index + m[0].length });
    seen.add(m[2]);
  }
  const host = replaceEdits(query, hostText).filter(e => !anchors.some(a => e.from < a.to && a.from < e.to));
  const cells: ReplaceAllPlan['cells'] = new Map();
  let cellCount = 0;
  for (const id of seen) {
    const rowIds = orderedRowIds(ydoc, id);
    const colIds = orderedColIds(ydoc, id);
    if (!rowIds || !colIds) continue;
    for (const rowId of rowIds) {
      for (const colId of colIds) {
        const text = getCellText(ydoc, id, rowId, colId);
        if (!text) continue;
        const edits = replaceEdits(query, text.toString());
        if (edits.length === 0) continue;
        cells.set(`${id}\u0000${cellKey(rowId, colId)}`, { text, edits });
        cellCount += edits.length;
      }
    }
  }
  return { host, cells, cellCount };
}

export interface ReplaceAllUndo {
  /** Revert the Replace-all. False (nothing popped) when it is no longer the top step. */
  undo(): boolean;
  destroy(): void;
}

/**
 * Apply a planned Replace-all in ONE transaction, as ONE step on the document-wide stack,
 * and return the panel's Undo for it — null when the doc has no stack (no live entity).
 * Edits go last-first per text so earlier offsets stay valid.
 *
 * INVARIANT(corruption): the panel Undo pops the shared stack ONLY while the Replace-all's
 * own item is on top, and `onStale` fires the moment it is not (an edit after it, a Ctrl+Z
 * that already reverted it). Why: popping any other item would undo an edit the line does
 * not describe — and a second revert of the Replace-all would duplicate its text.
 */
export function applyReplaceAll(
  ydoc: Y.Doc, hostText: Y.Text, plan: ReplaceAllPlan, onStale: () => void = () => {},
): ReplaceAllUndo | null {
  const targets: { text: Y.Text; edits: ReplaceEdit[] }[] = [];
  if (plan.host.length) targets.push({ text: hostText, edits: plan.host });
  targets.push(...plan.cells.values());
  const manager = documentUndo(ydoc);
  manager?.stopCapturing();
  ydoc.transact(() => {
    for (const { text, edits } of targets) {
      for (let i = edits.length - 1; i >= 0; i--) {
        const e = edits[i];
        text.delete(e.from, e.to - e.from);
        if (e.insert) text.insert(e.from, e.insert);
      }
    }
  }, REPLACE_ALL_ORIGIN);
  if (!manager) return null;
  manager.stopCapturing();
  const item = manager.undoStack[manager.undoStack.length - 1];
  const onTop = () => manager.undoStack[manager.undoStack.length - 1] === item;
  let alive = true;
  const destroy = () => {
    if (!alive) return;
    alive = false;
    manager.off('stack-item-added', check);
    manager.off('stack-item-popped', check);
  };
  // Stale is final: once anything else took the top, the line never comes back.
  function check() {
    if (onTop()) return;
    destroy();
    onStale();
  }
  manager.on('stack-item-added', check);
  manager.on('stack-item-popped', check);
  return {
    undo: () => {
      if (!alive || !item || !onTop()) return false;
      destroy();
      manager.undo();
      return true;
    },
    destroy,
  };
}

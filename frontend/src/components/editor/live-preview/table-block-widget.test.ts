/**
 * Tests for TableBlockWidget — armed (double-click-to-confirm) delete buttons.
 *
 * The full widget mount (toDOM) builds a nested CM6 EditorView per cell, which is heavy
 * and pulls the whole render-bundle into jsdom. The armed behavior lives entirely in the
 * toolbar DOM + instance state, so these tests drive `buildToolbar` directly with a real
 * Yjs model and the widget's own private armed helpers, reached via a typed access cast.
 * This covers the same observable contract a user sees (button class swap, model mutation,
 * timer/mouseleave/mutual disarm) without the editor mount.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import * as Y from 'yjs';
import type { EditorView } from '@codemirror/view';
import { createTable, readTableModel } from './table-block-model';
import { TableBlockWidget } from './table-block-widget';
import { setEntityHandle } from '../../../collab/active-handle-registry';
import { releaseHandle, publishHandle, setViewEntity } from '../../../editor/active-editor';
import type { EntityYjsState } from '../../../collab/yjs-provider';

type Kind = 'row' | 'col';

interface ArmedWidget {
  doc: Y.Doc;
  armedRow: boolean;
  armedCol: boolean;
  toolbarEl: HTMLElement | null;
  buildToolbar(): HTMLElement;
  arm(kind: Kind): void;
  disarm(kind: Kind): void;
}

const ARMED_CLASS = 'bg-red text-white';
const ACT = { row: 'delete-row', col: 'delete-col' };

function setup(matrix: string[][]): { widget: ArmedWidget; doc: Y.Doc; id: string } {
  const doc = new Y.Doc();
  const id = createTable(doc, matrix);
  const widget = new TableBlockWidget(id, 't') as unknown as ArmedWidget;
  // Wire the instance the same way toDOM does, minus the editor mount.
  widget.doc = doc;
  return { widget, doc, id };
}

function deleteBtn(widget: ArmedWidget, kind: Kind): HTMLElement {
  const toolbar = widget.buildToolbar();
  // render() assigns this; mirror it so arm()/disarm() can locate the live button nodes.
  widget.toolbarEl = toolbar;
  return toolbar.querySelector(`[data-act="${ACT[kind]}"]`) as HTMLElement;
}

/** Fire a toolbar mousedown exactly as the user would (cancelable; widget preventDefaults). */
function fireMousedown(btn: HTMLElement): void {
  btn.dispatchEvent(new MouseEvent('mousedown', { cancelable: true, bubbles: true }));
}

function isArmed(btn: HTMLElement): boolean {
  return btn.className.includes(ARMED_CLASS);
}

describe('TableBlockWidget — armed delete buttons', () => {
  beforeEach(() => {
    vi.useRealTimers();
  });

  describe('delete column', () => {
    it('1st activation arms (red), 2nd activation removes the column', () => {
      const { widget, doc, id } = setup([['a', 'b']]);
      const btn = deleteBtn(widget, 'col');
      // 1st → arms, model untouched.
      fireMousedown(btn);
      expect(isArmed(btn)).toBe(true);
      expect(widget.armedCol).toBe(true);
      expect(readTableModel(doc, id)!.rows[0]).toEqual(['a', 'b']);
      // 2nd → deletes the focused cell's column (col 0 by default), disarms.
      fireMousedown(btn);
      expect(readTableModel(doc, id)!.rows[0]).toEqual(['b']);
      expect(isArmed(btn)).toBe(false);
      expect(widget.armedCol).toBe(false);
    });

    it('does not arm or mutate at minimum size (1 column)', () => {
      const { widget, doc, id } = setup([['only']]);
      const btn = deleteBtn(widget, 'col');
      fireMousedown(btn);
      expect(isArmed(btn)).toBe(false);
      expect(widget.armedCol).toBe(false);
      // Still a single column — no mutation.
      expect(readTableModel(doc, id)!.rows[0]).toEqual(['only']);
      // A second activation is also a no-op (never armed).
      fireMousedown(btn);
      expect(readTableModel(doc, id)!.rows[0]).toEqual(['only']);
    });
  });

  describe('delete row', () => {
    it('1st activation arms (red), 2nd activation removes the row', () => {
      const { widget, doc, id } = setup([['h'], ['r1'], ['r2']]);
      const btn = deleteBtn(widget, 'row');
      fireMousedown(btn);
      expect(isArmed(btn)).toBe(true);
      expect(widget.armedRow).toBe(true);
      expect(readTableModel(doc, id)!.rows.length).toBe(3);
      // Deletes the focused row (row 0 by default).
      fireMousedown(btn);
      expect(readTableModel(doc, id)!.rows.length).toBe(2);
      expect(isArmed(btn)).toBe(false);
    });

    it('does not arm or mutate at minimum size (1 row)', () => {
      const { widget, doc, id } = setup([['solo']]);
      const btn = deleteBtn(widget, 'row');
      fireMousedown(btn);
      expect(isArmed(btn)).toBe(false);
      expect(widget.armedRow).toBe(false);
      expect(readTableModel(doc, id)!.rows.length).toBe(1);
    });
  });

  describe('disarm', () => {
    it('auto-disarms after ARM_TIMEOUT (2000ms)', () => {
      vi.useFakeTimers();
      const { widget } = setup([['a', 'b']]);
      const btn = deleteBtn(widget, 'col');
      fireMousedown(btn);
      expect(widget.armedCol).toBe(true);
      vi.advanceTimersByTime(1999);
      expect(widget.armedCol).toBe(true);
      vi.advanceTimersByTime(1);
      expect(widget.armedCol).toBe(false);
      expect(isArmed(btn)).toBe(false);
    });

    it('disarms on mouseleave', () => {
      const { widget } = setup([['a', 'b']]);
      const btn = deleteBtn(widget, 'col');
      fireMousedown(btn);
      expect(isArmed(btn)).toBe(true);
      btn.dispatchEvent(new MouseEvent('mouseleave', { bubbles: true }));
      expect(widget.armedCol).toBe(false);
      expect(isArmed(btn)).toBe(false);
    });

    it('mutual-disarm: arming col clears an armed row (no two red buttons)', () => {
      const { widget } = setup([['a', 'b'], ['c', 'd']]);
      const toolbar = widget.buildToolbar();
      widget.toolbarEl = toolbar;
      const rowBtn = toolbar.querySelector(`[data-act="${ACT.row}"]`) as HTMLElement;
      const colBtn = toolbar.querySelector(`[data-act="${ACT.col}"]`) as HTMLElement;
      // Arm row first.
      fireMousedown(rowBtn);
      expect(isArmed(rowBtn)).toBe(true);
      expect(widget.armedRow).toBe(true);
      // Arm col → row must disarm (class swapped on the same toolbar's row node).
      fireMousedown(colBtn);
      expect(widget.armedCol).toBe(true);
      expect(widget.armedRow).toBe(false);
      expect(isArmed(rowBtn)).toBe(false);
      expect(isArmed(colBtn)).toBe(true);
    });
  });

  describe('armed visual survives a toolbar rebuild (collab structural edit)', () => {
    it('re-applies the armed class when buildToolbar runs while armed', () => {
      const { widget } = setup([['a', 'b']]);
      // Arm col via the first toolbar.
      const first = widget.buildToolbar();
      widget.toolbarEl = first;
      fireMousedown(first.querySelector(`[data-act="${ACT.col}"]`) as HTMLElement);
      expect(widget.armedCol).toBe(true);
      // A collaborative structural change triggers a rebuild — armed state persists.
      const rebuilt = widget.buildToolbar();
      widget.toolbarEl = rebuilt;
      const rebuiltCol = rebuilt.querySelector(`[data-act="${ACT.col}"]`) as HTMLElement;
      expect(isArmed(rebuiltCol)).toBe(true);
    });
  });

  describe('destroy clears arm timers', () => {
    it('destroy() disarms both timers without a dangling auto-disarm', () => {
      vi.useFakeTimers();
      const { widget, doc, id } = setup([['a', 'b']]);
      const toolbar = widget.buildToolbar();
      widget.toolbarEl = toolbar;
      // Arm both (mutual-disarm means only col stays armed, but both timers get touched).
      widget.arm('row');
      widget.arm('col');
      expect(widget.armedCol).toBe(true);
      const full = widget as unknown as { destroy(): void };
      full.destroy();
      // Advancing timers past ARM_TIMEOUT must not fire any callback on the dead widget.
      expect(() => vi.advanceTimersByTime(5000)).not.toThrow();
      // Model untouched — no auto-disarm side effect reached the doc.
      expect(readTableModel(doc, id)!.rows[0]).toEqual(['a', 'b']);
    });
  });
});

describe('TableBlockWidget — entity late-bind (split view)', () => {
  interface BindableWidget {
    doc: Y.Doc | null;
    toDOM(view: unknown): HTMLElement;
    destroy(): void;
  }

  /** Minimal view stand-in: facet yields nothing (no snapshot doc), not read-only.
   *  The late-bind path only reads state.facet/state.readOnly at mount. */
  function mockView() {
    return {
      state: { facet: () => null, readOnly: false },
      requestMeasure: () => {},
    };
  }

  function makeEntityHandle(doc: Y.Doc): EntityYjsState {
    return { ydoc: doc } as unknown as EntityYjsState;
  }

  afterEach(() => {
    releaseHandle();
    setEntityHandle('doc-1', null);
    setEntityHandle('e-1', null);
  });

  it('mounts blank with no handle, binds when the ENTITY handle lands (no slot involved)', () => {
    const w = new TableBlockWidget('t-1', 't') as unknown as BindableWidget;
    const view = mockView();
    setViewEntity(view as unknown as EditorView, 'doc-1');
    const wrap = w.toDOM(view);
    // No handle anywhere: blank — and NO "remove anchor?" affordance (healthy table,
    // just not bound yet).
    expect(w.doc).toBeNull();
    expect(wrap.children.length).toBe(0);

    // The entity's connection lands AFTER the widget mounted.
    const ydoc = new Y.Doc();
    setEntityHandle('doc-1', makeEntityHandle(ydoc));

    // Bound + rendered without any scroll/recreate: the model is absent, so the
    // bound render shows the missing-model placeholder (evidence it rendered).
    expect(w.doc).toBe(ydoc);
    expect(wrap.querySelector('.cm-table-block-error')).not.toBeNull();

    w.destroy();
    ydoc.destroy();
  });

  it('NEVER binds the first non-null SLOT value (foreign column) — no renderMissing', () => {
    // A FOREIGN handle (the other column's doc, holding a real table) owns the slot.
    const foreignDoc = new Y.Doc();
    createTable(foreignDoc, [['f1', 'f2']]);
    publishHandle(makeEntityHandle(foreignDoc));

    // The view renders entity e-1; e-1 has no handle yet → the widget waits.
    const view = mockView();
    setViewEntity(view as unknown as EditorView, 'e-1');
    const w = new TableBlockWidget('t-1', 't') as unknown as BindableWidget;
    const wrap = w.toDOM(view);

    // The foreign slot handle must NOT be bound — no shape-miss renderMissing (the
    // destructive "remove anchor?" affordance) on this healthy-table view.
    expect(w.doc).toBeNull();
    expect(wrap.children.length).toBe(0);
    expect(wrap.querySelector('.cm-table-block-error')).toBeNull();

    w.destroy();
    foreignDoc.destroy();
  });

  it('a REPLACED entity handle re-binds the widget onto the new ydoc', () => {
    const w = new TableBlockWidget('t-1', 't') as unknown as BindableWidget;
    const view = mockView();
    setViewEntity(view as unknown as EditorView, 'doc-1');
    const wrap = w.toDOM(view);

    // First bind: the model is absent → missing-model placeholder.
    const ydoc1 = new Y.Doc();
    setEntityHandle('doc-1', makeEntityHandle(ydoc1));
    expect(w.doc).toBe(ydoc1);
    expect(wrap.querySelector('.cm-table-block-error')).not.toBeNull();

    // Re-join lands a NEW ydoc for the same entity: the widget must follow —
    // a bound widget left on the old ydoc renders stale and its edits reach no peer.
    const ydoc2 = new Y.Doc();
    setEntityHandle('doc-1', makeEntityHandle(ydoc2));
    expect(w.doc).toBe(ydoc2);

    // The old map observer is dropped: a mutation on the LEFT ydoc must not re-render
    // (no throw from a destroyed-doc observer, no stale DOM rebuild).
    ydoc1.getMap('tables').set('x', 'y');
    expect(w.doc).toBe(ydoc2);

    w.destroy();
    ydoc1.destroy();
    ydoc2.destroy();
  });
});

// @vitest-environment jsdom
/**
 * Tests for linkClickExtension — single-click navigation in read-only (revealAtCursor=false)
 * vs the two-click dance preserved in editable (revealAtCursor default true).
 *
 * SYSTEM: editor-plugins — link click handler. Mounts a minimal EditorView with
 * linkClickExtension (± the revealAtCursor facet), stubs view.posAtCoords (jsdom has no
 * layout), dispatches a real MouseEvent('click') on contentDOM, and asserts on the mocked
 * events.emit (+ a store toast spy for the broken-link path). Pattern from copy-handler.test.ts.
 * The gesture-gate suite dispatches a real mousedown (press) before the click to seed
 * the drag/selection gate's inputs.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { EditorView } from '@codemirror/view';
import { EditorState, EditorSelection, type Extension } from '@codemirror/state';
import { linkClickExtension } from './editor-plugins';
import { revealAtCursor } from '../components/editor/live-preview';
import { validDocIds } from '../components/editor/live-preview/link-validity';
import { emit } from '../events';

vi.mock('../events', () => ({ emit: vi.fn() }));

/** Minimal EditorView carrying the link-click extension + any extra facets. */
function makeView(doc: string, extra: Extension[] = []): EditorView {
  return new EditorView({
    state: EditorState.create({
      doc,
      extensions: [linkClickExtension, ...extra],
    }),
    parent: document.body,
  });
}

/** Dispatch a mousedown on contentDOM, seeding the gesture record (coords).
 *  detail:1 marks a real single mousedown — without it CM6's built-in handler reads the
 *  gesture as a triple click and selects the whole line. */
function press(view: EditorView, opts: { x?: number; y?: number } = {}): void {
  view.contentDOM.dispatchEvent(
    new MouseEvent('mousedown', {
      bubbles: true,
      cancelable: true,
      detail: 1,
      clientX: opts.x ?? 0,
      clientY: opts.y ?? 0,
    }),
  );
}

/** Stub posAtCoords to a known doc offset, then dispatch a click on contentDOM.
 *  `opts.press` dispatches a mousedown first (seeding gesture coords);
 *  `opts.x/y` set the click's own coords. */
function click(
  view: EditorView,
  pos: number,
  opts: {
    meta?: boolean;
    ctrl?: boolean;
    shift?: boolean;
    alt?: boolean;
    x?: number;
    y?: number;
    press?: { x?: number; y?: number };
  } = {},
): void {
  view.posAtCoords = () => pos;
  if (opts.press) press(view, opts.press);
  view.contentDOM.dispatchEvent(
    new MouseEvent('click', {
      bubbles: true,
      cancelable: true,
      clientX: opts.x ?? 0,
      clientY: opts.y ?? 0,
      metaKey: !!opts.meta,
      ctrlKey: !!opts.ctrl,
      shiftKey: !!opts.shift,
      altKey: !!opts.alt,
    }),
  );
}

describe('linkClickExtension — read-only (revealAtCursor=false)', () => {
  beforeEach(() => {
    validDocIds.clear();
    vi.mocked(emit).mockClear();
  });

  it('navigates on the FIRST click for a valid doc link', () => {
    validDocIds.add('abc');
    const view = makeView('[Doc](doc:abc)', [revealAtCursor.of(false)]);
    click(view, 2); // offset inside the visible "Doc" label
    expect(emit).toHaveBeenCalledTimes(1);
    expect(emit).toHaveBeenCalledWith('navigate-to-document', { documentId: 'abc' });
    view.destroy();
  });

  it('shows a toast and does NOT navigate for a broken doc link', async () => {
    const { useAppStore } = await import('../store/app-store');
    const spy = vi.spyOn(useAppStore.getState(), 'showToast');
    const view = makeView('[Doc](doc:gone)', [revealAtCursor.of(false)]);
    click(view, 2);
    expect(spy).toHaveBeenCalled();
    expect(emit).not.toHaveBeenCalled();
    spy.mockRestore();
    view.destroy();
  });

  it('does NOT navigate when the click lands outside the link span', () => {
    validDocIds.add('abc');
    // doc link occupies [0, 15); click offset 18 lands in " trailing".
    const view = makeView('[Doc](doc:abc) trailing', [revealAtCursor.of(false)]);
    click(view, 18);
    expect(emit).not.toHaveBeenCalled();
    view.destroy();
  });
});

describe('linkClickExtension — editable (revealAtCursor default true)', () => {
  beforeEach(() => {
    validDocIds.clear();
    validDocIds.add('abc');
    vi.mocked(emit).mockClear();
  });

  it('does NOT navigate on a single plain click (two-click dance preserved)', () => {
    const view = makeView('[Doc](doc:abc)'); // default reveal = true
    click(view, 2);
    expect(emit).not.toHaveBeenCalled();
    view.destroy();
  });

  it('navigates on Cmd+click (meta path, first click)', () => {
    const view = makeView('[Doc](doc:abc)');
    click(view, 2, { meta: true });
    expect(emit).toHaveBeenCalledWith('navigate-to-document', { documentId: 'abc' });
    view.destroy();
  });
});

describe('linkClickExtension — gesture gate (a drag/selection gesture is not a click)', () => {
  beforeEach(() => {
    validDocIds.clear();
    validDocIds.add('abc');
    vi.mocked(emit).mockClear();
  });

  it('12px movement from mousedown to click: no navigate in read-only', () => {
    const view = makeView('[Doc](doc:abc)', [revealAtCursor.of(false)]);
    click(view, 2, { x: 12, press: { x: 0 } });
    expect(emit).not.toHaveBeenCalled();
    view.destroy();
  });

  it('12px movement: no navigate even when the link was expanded (editable)', () => {
    const view = makeView('[Doc](doc:abc)');
    view.dispatch({ selection: { anchor: 2 } }); // cursor inside the label = expanded
    click(view, 2, { x: 12, press: { x: 0 } });
    expect(emit).not.toHaveBeenCalled();
    view.destroy();
  });

  it('same coords but a non-empty selection at click time: no action (drag-select)', () => {
    const view = makeView('[Doc](doc:abc)', [revealAtCursor.of(false)]);
    press(view, { x: 0 });
    view.dispatch({ selection: { anchor: 1, head: 3 } }); // what the drag selected
    click(view, 2, { x: 0 });
    expect(emit).not.toHaveBeenCalled();
    view.destroy();
  });

  it('shift+click and alt+click: no action even in read-only', () => {
    const view = makeView('[Doc](doc:abc)', [revealAtCursor.of(false)]);
    click(view, 2, { shift: true, press: { x: 0 }, x: 0 });
    click(view, 2, { alt: true, press: { x: 0 }, x: 0 });
    expect(emit).not.toHaveBeenCalled();
    view.destroy();
  });

  it('multi-range (rectangular) selection: no action', () => {
    const view = makeView('[Doc](doc:abc) trailing', [
      revealAtCursor.of(false),
      EditorState.allowMultipleSelections.of(true),
    ]);
    press(view, { x: 0 });
    view.dispatch({
      selection: EditorSelection.create([EditorSelection.range(0, 2), EditorSelection.range(16, 18)], 0),
    });
    click(view, 2, { x: 0 });
    expect(emit).not.toHaveBeenCalled();
    view.destroy();
  });

  it('Cmd+click with 12px movement: still suppressed (gate outranks modifiers)', () => {
    const view = makeView('[Doc](doc:abc)', [revealAtCursor.of(false)]);
    click(view, 2, { meta: true, x: 12, press: { x: 0 } });
    expect(emit).not.toHaveBeenCalled();
    view.destroy();
  });

  it('movement ≤4px + empty selection: read-only still navigates', () => {
    const view = makeView('[Doc](doc:abc)', [revealAtCursor.of(false)]);
    click(view, 2, { x: 4, press: { x: 0 } });
    expect(emit).toHaveBeenCalledTimes(1);
    expect(emit).toHaveBeenCalledWith('navigate-to-document', { documentId: 'abc' });
    view.destroy();
  });

  it('movement ≤4px + empty selection: editable dance preserved (first click reveals, second navigates)', () => {
    const view = makeView('[Doc](doc:abc)');
    click(view, 2, { x: 4, press: { x: 0 } }); // first plain click: reveal only
    expect(emit).not.toHaveBeenCalled();
    view.dispatch({ selection: { anchor: 2 } }); // the reveal placed the cursor inside the label
    click(view, 2, { x: 4, press: { x: 4 } }); // second plain click: expanded → navigate
    expect(emit).toHaveBeenCalledTimes(1);
    view.destroy();
  });

  it('pre-existing selection collapsed by the gesture still navigates (read-only)', () => {
    const view = makeView('[Doc](doc:abc)', [revealAtCursor.of(false)]);
    press(view, { x: 0 });
    view.dispatch({ selection: { anchor: 1, head: 3 } }); // pre-existing selection
    // CM6 collapses the selection during mouseup after a plain click; applied explicitly.
    view.dispatch({ selection: { anchor: 2 } });
    click(view, 2, { x: 0 });
    expect(emit).toHaveBeenCalledTimes(1);
    view.destroy();
  });
});

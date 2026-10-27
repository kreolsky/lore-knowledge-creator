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

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { EditorView, Decoration } from '@codemirror/view';
import { EditorState, EditorSelection, StateField, type Extension } from '@codemirror/state';
import { TableWidget } from '../components/editor/live-preview/table-widget';
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
function press(
  view: EditorView,
  opts: { x?: number; y?: number; ctrl?: boolean; target?: Element } = {},
): void {
  (opts.target ?? view.contentDOM).dispatchEvent(
    new MouseEvent('mousedown', {
      bubbles: true,
      cancelable: true,
      detail: 1,
      clientX: opts.x ?? 0,
      clientY: opts.y ?? 0,
      ctrlKey: !!opts.ctrl,
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
    detail?: number;
    target?: Element;
    press?: { x?: number; y?: number; ctrl?: boolean };
  } = {},
): void {
  view.posAtCoords = () => pos;
  if (opts.press) press(view, { ...opts.press, target: opts.target });
  (opts.target ?? view.contentDOM).dispatchEvent(
    new MouseEvent('click', {
      bubbles: true,
      cancelable: true,
      detail: opts.detail ?? 0,
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

  it('double-click whose first click revealed the link: navigates despite the word selection', () => {
    const view = makeView('[Doc](doc:abc)');
    view.dispatch({ selection: { anchor: 2 } }); // first click revealed: cursor inside the label
    press(view, { x: 0 });
    view.dispatch({ selection: { anchor: 1, head: 4 } }); // CM6 selects the word on the 2nd mousedown
    click(view, 2, { x: 0, detail: 2 });
    expect(emit).toHaveBeenCalledTimes(1);
    expect(emit).toHaveBeenCalledWith('navigate-to-document', { documentId: 'abc' });
    view.destroy();
  });

  it('double-click on a collapsed link (cursor elsewhere): no navigate', () => {
    const view = makeView('[Doc](doc:abc) trailing');
    view.dispatch({ selection: { anchor: 20 } });
    press(view, { x: 0 });
    view.dispatch({ selection: { anchor: 1, head: 4 } });
    click(view, 2, { x: 0, detail: 2 });
    expect(emit).not.toHaveBeenCalled();
    view.destroy();
  });

  it('double-click in read-only: no second navigate (the first click already navigated)', () => {
    const view = makeView('[Doc](doc:abc)', [revealAtCursor.of(false)]);
    press(view, { x: 0 });
    view.dispatch({ selection: { anchor: 1, head: 4 } });
    click(view, 2, { x: 0, detail: 2 });
    expect(emit).not.toHaveBeenCalled();
    view.destroy();
  });

  it('triple-click selection inside an expanded link: no action', () => {
    const view = makeView('[Doc](doc:abc)');
    view.dispatch({ selection: { anchor: 2 } });
    press(view, { x: 0 });
    view.dispatch({ selection: { anchor: 0, head: 14 } });
    click(view, 2, { x: 0, detail: 3 });
    expect(emit).not.toHaveBeenCalled();
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

describe('linkClickExtension — Mod+click on a link (non-mac jsdom: mod = Ctrl)', () => {
  let openSpy: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    validDocIds.clear();
    vi.mocked(emit).mockClear();
    openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
  });
  afterEach(() => openSpy.mockRestore());

  const multi = EditorState.allowMultipleSelections.of(true);

  it('Ctrl+click on a collapsed external link: navigates once, adds no range, keeps the caret', () => {
    const view = makeView('see [x](https://e.com) end', [multi]);
    view.dispatch({ selection: { anchor: 25 } });
    click(view, 5, { ctrl: true, press: { ctrl: true } });
    expect(openSpy).toHaveBeenCalledTimes(1);
    expect(view.state.selection.ranges.length).toBe(1);
    expect(view.state.selection.main.head).toBe(25);
    view.destroy();
  });

  it('Ctrl+click on the url part of an expanded link: navigates, one range', () => {
    const view = makeView('see [x](https://e.com) end', [multi]);
    view.dispatch({ selection: { anchor: 5 } }); // caret inside the label = expanded
    click(view, 12, { ctrl: true, press: { ctrl: true } });
    expect(openSpy).toHaveBeenCalledTimes(1);
    expect(view.state.selection.ranges.length).toBe(1);
    view.destroy();
  });

  it('Ctrl+click on plain text: adds a second cursor', () => {
    const view = makeView('plain words here', [multi]);
    // CM6's own mouse selection resolves via posAndSideAtCoords (unstubbed: 0) — keep the
    // caret off 0 so the added range does not merge into it.
    view.dispatch({ selection: { anchor: 15 } });
    click(view, 8, { ctrl: true, press: { ctrl: true } });
    expect(view.state.selection.ranges.length).toBe(2);
    expect(openSpy).not.toHaveBeenCalled();
    view.destroy();
  });
});

/** A view whose whole doc is one rendered GFM table, decorated as tableRenderField does:
 *  whole-line block replace, rawText from the table node (after row 0's indent). */
function tableView(doc: string, extra: Extension[] = []): EditorView {
  const indent = /^\s*/.exec(doc)![0].length;
  const field = StateField.define({
    create: (state) => Decoration.set([
      Decoration.replace({ widget: new TableWidget(state.doc.sliceString(indent)), block: true })
        .range(0, state.doc.length),
    ]),
    update: (deco) => deco,
    provide: (f) => EditorView.decorations.from(f),
  });
  return makeView(doc, [field, ...extra]);
}

function stubCaret(node: Node, offset: number): void {
  (document as unknown as { caretPositionFromPoint: unknown }).caretPositionFromPoint =
    () => ({ offsetNode: node, offset });
}

describe('linkClickExtension — rendered GFM table', () => {
  let openSpy: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    validDocIds.clear();
    validDocIds.add('abc');
    vi.mocked(emit).mockClear();
    openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
  });
  afterEach(() => {
    openSpy.mockRestore();
    delete (document as unknown as { caretPositionFromPoint?: unknown }).caretPositionFromPoint;
  });

  const DOC = '| h1 | h2 |\n|---|---|\n| [x](https://e.com) | bc |\n| [D](doc:abc) | [G](doc:gone) |';

  it('plain click on an external link navigates; widget stays, selection unchanged', () => {
    const view = tableView(DOC);
    view.dispatch({ selection: { anchor: DOC.length } });
    const link = view.contentDOM.querySelectorAll('td')[0].querySelector('[data-src-off]')!;
    click(view, 0, { target: link, press: {} });
    expect(openSpy).toHaveBeenCalledTimes(1);
    expect(view.contentDOM.querySelector('.cm-table-widget-wrap')).not.toBeNull();
    expect(view.state.selection.main.head).toBe(DOC.length);
    view.destroy();
  });

  it('plain click on an internal doc link navigates to the doc', () => {
    const view = tableView(DOC);
    const link = view.contentDOM.querySelectorAll('td')[2].querySelector('[data-src-off]')!;
    click(view, 0, { target: link, press: {} });
    expect(emit).toHaveBeenCalledWith('navigate-to-document', { documentId: 'abc' });
    view.destroy();
  });

  it('plain click on a broken doc link shows the broken toast', async () => {
    const { useAppStore } = await import('../store/app-store');
    const spy = vi.spyOn(useAppStore.getState(), 'showToast');
    const view = tableView(DOC);
    const link = view.contentDOM.querySelectorAll('td')[3].querySelector('[data-src-off]')!;
    click(view, 0, { target: link, press: {} });
    expect(spy).toHaveBeenCalled();
    expect(emit).not.toHaveBeenCalled();
    spy.mockRestore();
    view.destroy();
  });

  it('plain click on a plain-text cell puts the caret at the clicked char', () => {
    const view = tableView(DOC);
    const td = view.contentDOM.querySelectorAll('td')[1];
    stubCaret(td.firstChild!, 1);
    click(view, 0, { target: td, press: {} });
    const line = view.state.doc.line(3);
    expect(view.state.selection.main.head).toBe(line.from + line.text.indexOf('bc') + 1);
    view.destroy();
  });

  it('a non-link click on the second body row lands on that row', () => {
    const view = tableView('| h |\n|---|\n| aa |\n| bb |');
    const td = view.contentDOM.querySelectorAll('td')[1];
    stubCaret(td.firstChild!, 0);
    click(view, 0, { target: td, press: {} });
    const line = view.state.doc.line(4);
    expect(view.state.selection.main.head).toBe(line.from + line.text.indexOf('bb'));
    view.destroy();
  });

  it('a table indented by 2 spaces maps header and body clicks exactly', () => {
    const doc = '  | h1 | h2 |\n  |---|---|\n  | p | qr |';
    const view = tableView(doc);
    const th = view.contentDOM.querySelectorAll('th')[1];
    stubCaret(th.firstChild!, 1);
    click(view, 0, { target: th, press: {} });
    expect(view.state.selection.main.head).toBe(doc.indexOf('h2') + 1);
    const td = view.contentDOM.querySelectorAll('td')[1];
    stubCaret(td.firstChild!, 1);
    click(view, 0, { target: td, press: {} });
    expect(view.state.selection.main.head).toBe(doc.indexOf('qr') + 1);
    view.destroy();
  });

  it('a drag starting in a table cell anchors the selection at the pressed char', () => {
    const view = tableView(DOC);
    const td = view.contentDOM.querySelectorAll('td')[1];
    stubCaret(td.firstChild!, 1);
    const line = view.state.doc.line(4);
    view.posAtCoords = () => line.from + 4;
    press(view, { target: td, x: 0 });
    document.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, buttons: 1, clientX: 30, clientY: 0 }));
    const l3 = view.state.doc.line(3);
    expect(view.state.selection.main.anchor).toBe(l3.from + l3.text.indexOf('bc') + 1);
    expect(view.state.selection.main.head).toBe(line.from + 4);
    view.destroy();
  });

  it('a drag starting in a table cell neither corrects the caret nor navigates', () => {
    const view = tableView(DOC);
    const link = view.contentDOM.querySelectorAll('td')[0].querySelector('[data-src-off]')!;
    view.posAtCoords = () => 0;
    press(view, { target: link, x: 0 });
    const head = view.state.selection.main.head;
    click(view, 0, { target: link, x: 10 });
    expect(openSpy).not.toHaveBeenCalled();
    expect(view.state.selection.main.head).toBe(head);
    view.destroy();
  });
});

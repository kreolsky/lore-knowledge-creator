/**
 * TDD: contract for useScrollToLine before implementation.
 *
 * SYSTEM: event-bus — 'scroll-to-line' (TOC click) → CM6 scrollIntoView.
 *
 * Regression this pins: the ONLY subscriber used to be useEditorEvents
 * (authed Editor), so TOC clicks on the public /s/:token viewer fired into
 * an empty bus. The hook is the shared listener both surfaces mount.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, useRef } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';

type EmitFn = typeof import('../events/event-bus').emit;
type UseScrollToLineFn = typeof import('./useScrollToLine').useScrollToLine;

let emit: EmitFn;
let useScrollToLine: UseScrollToLineFn;

// Headings at lines 1 / 5 / 9; trailing \n → doc.lines === 10.
const DOC = '# Title\n\npara\n\n## Section\n\nmore text\n\n### Deep\n';

let container: HTMLDivElement;
let root: Root;
let view: EditorView;
let dispatchSpy: ReturnType<typeof vi.spyOn>;

/** Probe whose ref is never assigned — view not yet created (doc switch remount). */
function NullRefProbe() {
  const ref = useRef<EditorView | null>(null);
  useScrollToLine({ editorViewRef: ref });
  return null;
}

beforeEach(async () => {
  vi.resetModules();
  const bus = await import('../events/event-bus');
  emit = bus.emit;
  useScrollToLine = (await import('./useScrollToLine')).useScrollToLine;

  view = new EditorView({
    state: EditorState.create({ doc: DOC }),
    parent: document.body,
  });
  dispatchSpy = vi.spyOn(view, 'dispatch');

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  view.destroy();
});

describe('useScrollToLine', () => {

  it('editorViewRef.current === null → no throw, no dispatch', () => {
    expect(() => {
      act(() => root.render(createElement(NullRefProbe)));
      act(() => emit('scroll-to-line', { line: 4 }));
    }).not.toThrow();
    expect(dispatchSpy).not.toHaveBeenCalled();
  });
});

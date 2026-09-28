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
let editorViewRef: React.RefObject<EditorView | null>;

/** Probe component mounting the hook with a ref pre-seeded with the view. */
function Probe({ enabled }: { enabled?: boolean }) {
  const ref = useRef<EditorView | null>(view);
  useScrollToLine({ editorViewRef: ref, enabled });
  return null;
}

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
  it('dispatches a scrollIntoView effect for a valid line', () => {
    act(() => root.render(createElement(Probe, { enabled: true })));
    act(() => emit('scroll-to-line', { line: 4 }));

    expect(dispatchSpy).toHaveBeenCalledOnce();
    const effects = dispatchSpy.mock.calls[0][0].effects;
    const effect = Array.isArray(effects) ? effects[0] : effects;
    expect(effect!.value.range.from).toBe(DOC_LINE(4));
    expect(effect!.value.y).toBe('start');
    expect(effect!.value.yMargin).toBe(60);
  });

  it('no dispatch for line < 1', () => {
    act(() => root.render(createElement(Probe, { enabled: true })));
    act(() => emit('scroll-to-line', { line: 0 }));
    expect(dispatchSpy).not.toHaveBeenCalled();
  });

  it('no dispatch for line > doc.lines', () => {
    act(() => root.render(createElement(Probe, { enabled: true })));
    act(() => emit('scroll-to-line', { line: 99 }));
    expect(dispatchSpy).not.toHaveBeenCalled();
  });

  it('enabled: false → no dispatch (secondary split column)', () => {
    act(() => root.render(createElement(Probe, { enabled: false })));
    act(() => emit('scroll-to-line', { line: 4 }));
    expect(dispatchSpy).not.toHaveBeenCalled();
  });

  it('editorViewRef.current === null → no throw, no dispatch', () => {
    expect(() => {
      act(() => root.render(createElement(NullRefProbe)));
      act(() => emit('scroll-to-line', { line: 4 }));
    }).not.toThrow();
    expect(dispatchSpy).not.toHaveBeenCalled();
  });
});

/** Offset of a 1-based line start in DOC (independent of the view under test). */
function DOC_LINE(line: number): number {
  return DOC.split('\n').slice(0, line - 1).join('\n').length + (line > 1 ? 1 : 0);
}

import type React from 'react';

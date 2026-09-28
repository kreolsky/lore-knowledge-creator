/** Tests for editor-observer unresolved transclusion scan. */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { createObserverExtension } from './editor-observer';
import { transcludeMap } from '../components/editor/live-preview';
import * as events from '../events';
import type React from 'react';

function makeView(doc: string): EditorView {
  const activeItemRef: React.RefObject<null> = { current: null };
  const refIsEmptySetterRef: React.RefObject<(e: boolean) => void> = { current: () => {} };
  const state = EditorState.create({
    doc,
    extensions: [createObserverExtension({ activeItemRef, refIsEmptySetterRef })],
  });
  return new EditorView({ state, parent: document.createElement('div') });
}

describe('editor-observer unresolved scan', () => {
  beforeEach(() => {
    transcludeMap.clear();
    vi.useFakeTimers();
  });
  afterEach(() => vi.useRealTimers());

  it('does NOT emit when a ref: image id is resolved', () => {
    transcludeMap.set('img1', { kind: 'ref-image', title: 'I', imageUrl: 'http://localhost/x.png' });
    const spy = vi.spyOn(events, 'emit');
    const view = makeView('![a](ref:img1)');
    view.dispatch({ changes: { from: view.state.doc.length, insert: ' ' } });
    vi.advanceTimersByTime(600);
    expect(spy).not.toHaveBeenCalledWith('unresolved-image-ref');
    spy.mockRestore();
  });

  it('emits for an unresolved ref: id', () => {
    const spy = vi.spyOn(events, 'emit');
    const view = makeView('![a](ref:nope)');
    view.dispatch({ changes: { from: view.state.doc.length, insert: ' ' } });
    vi.advanceTimersByTime(600);
    expect(spy).toHaveBeenCalledWith('unresolved-image-ref');
    spy.mockRestore();
  });

  it('does NOT emit when a doc: transclusion id is resolved in transcludeMap', () => {
    transcludeMap.set('doc1', { kind: 'doc', title: 'D', content: 'c' });
    const spy = vi.spyOn(events, 'emit');
    const view = makeView('![t](doc:doc1)');
    view.dispatch({ changes: { from: view.state.doc.length, insert: ' ' } });
    vi.advanceTimersByTime(600);
    expect(spy).not.toHaveBeenCalledWith('unresolved-image-ref');
    spy.mockRestore();
  });

  it('emits for an unresolved doc: transclusion id', () => {
    const spy = vi.spyOn(events, 'emit');
    const view = makeView('![t](doc:missing)');
    view.dispatch({ changes: { from: view.state.doc.length, insert: ' ' } });
    vi.advanceTimersByTime(600);
    expect(spy).toHaveBeenCalledWith('unresolved-image-ref');
    spy.mockRestore();
  });

  it('does NOT emit for a plain doc link without ! prefix', () => {
    const spy = vi.spyOn(events, 'emit');
    const view = makeView('[t](doc:someid)');
    view.dispatch({ changes: { from: view.state.doc.length, insert: ' ' } });
    vi.advanceTimersByTime(600);
    expect(spy).not.toHaveBeenCalledWith('unresolved-image-ref');
    spy.mockRestore();
  });
});

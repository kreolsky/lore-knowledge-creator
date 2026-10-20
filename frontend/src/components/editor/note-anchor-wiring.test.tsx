/** The live editor repaints note anchors once its collab binding goes live, not only on a list change. */
// @vitest-environment jsdom
import { describe, it, expect, afterEach } from 'vitest';
import { createElement, act, useRef, useEffect, type RefObject } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import * as Y from 'yjs';
import { buildNoteAnchor } from '../../collab/note-anchor-wire';
import { setEntityHandle } from '../../collab/active-handle-registry';
import { noteHighlightExtension } from '../../editor/note-highlight';
import { makeNoteAnchorResolver, useNotesChangedDispatch } from './note-anchor-wiring';
import { useNoteChatStore } from '../../store/note-chat-store';
import type { ChatSession } from '../../types';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const DOC = 'doc-live';
const TEXT = 'Привет 😀 мир 😀 пока';

let root: Root | null = null;
let view: EditorView | null = null;

afterEach(() => {
  const r = root;
  if (r) act(() => r.unmount());
  root = null;
  view?.destroy();
  view = null;
  setEntityHandle(DOC, null);
  useNoteChatStore.getState().reset();
});

function Host({ viewRef, live }: { viewRef: RefObject<EditorView | null>; live: boolean }) {
  useNotesChangedDispatch(viewRef, live);
  return null;
}

function Harness({ live }: { live: boolean }) {
  const ref = useRef<EditorView | null>(view);
  useEffect(() => { ref.current = view; });
  return createElement(Host, { viewRef: ref, live });
}

describe('useNotesChangedDispatch', () => {
  it('paints a note whose list arrived before the ydoc, once the binding goes live', () => {
    // The anchor is built on the doc the collab binding will register later.
    const ydoc = new Y.Doc();
    const ytext = ydoc.getText('content');
    ytext.insert(0, TEXT);
    const from = TEXT.indexOf('мир');
    const anchor = buildNoteAnchor(ytext, from, from + 3);

    view = new EditorView({
      state: EditorState.create({
        doc: TEXT,
        extensions: noteHighlightExtension(makeNoteAnchorResolver(() => DOC)),
      }),
      parent: document.createElement('div'),
    });
    const host = document.createElement('div');
    root = createRoot(host);

    // 1. The note list lands first: no handle registered yet, nothing to resolve.
    useNoteChatStore.setState({
      sessions: [{
        session_id: 'n1', document_id: DOC, reference_id: null,
        anchor_rel_start: anchor.relStart, anchor_rel_end: anchor.relEnd,
      } as unknown as ChatSession],
    });
    act(() => root!.render(createElement(Harness, { live: false })));
    expect(view.dom.querySelectorAll('.cm-note-anchor')).toHaveLength(0);

    // 2. The binding registers the synced ydoc — the text is unchanged, so no
    //    doc-change transaction repaints.
    setEntityHandle(DOC, { ydoc, ytext } as never);
    act(() => root!.render(createElement(Harness, { live: false })));
    expect(view.dom.querySelectorAll('.cm-note-anchor')).toHaveLength(0);

    // 3. The binding goes live: the highlight repaints over the fragment.
    act(() => root!.render(createElement(Harness, { live: true })));
    const marks = view.dom.querySelectorAll('.cm-note-anchor');
    expect(marks).toHaveLength(1);
    expect(marks[0].textContent).toBe('мир');
  });
});

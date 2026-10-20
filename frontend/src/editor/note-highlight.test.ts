/** Note highlight StateField: paints resolver ranges, cache gate, findNoteAnchorRange. */
import { describe, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';

vi.mock('../components/editor/live-preview/link-types', () => ({ openNoteThread: vi.fn() }));
vi.mock('./editor-plugins', () => ({
  isPanelPreviewView: () => false,
  noteUnavailableInPreviewToast: vi.fn(),
}));

import { findNoteAnchorRange, noteHighlightExtension, notesChanged, type NoteAnchorRange } from './note-highlight';

function makeView(doc: string, resolver: () => NoteAnchorRange[]): EditorView {
  return new EditorView({
    state: EditorState.create({ doc, extensions: [noteHighlightExtension(resolver)] }),
    parent: document.body,
  });
}

describe('note highlight', () => {
  it('paints each resolved range with its note id', () => {
    const view = makeView('Привет мир 😀 конец', () => [{ id: 'n1', from: 7, to: 10 }]);
    const mark = view.contentDOM.querySelector('.cm-note-anchor');
    expect(mark?.getAttribute('data-note-id')).toBe('n1');
    expect(mark?.textContent).toBe('мир');
    expect(findNoteAnchorRange(view.state, 'n1')).toEqual({ id: 'n1', from: 7, to: 10 });
    view.destroy();
  });

  it('drops empty and out-of-bounds ranges', () => {
    const view = makeView('мир😀', () => [
      { id: 'empty', from: 1, to: 1 },
      { id: 'oob', from: 2, to: 99 },
    ]);
    expect(view.contentDOM.querySelector('.cm-note-anchor')).toBeNull();
    expect(findNoteAnchorRange(view.state, 'oob')).toBeNull();
    view.destroy();
  });

  it('re-resolves only on a doc change or a notesChanged annotation', () => {
    const resolver = vi.fn(() => [] as NoteAnchorRange[]);
    const view = makeView('мир😀', resolver);
    const calls = resolver.mock.calls.length;
    view.dispatch({ selection: { anchor: 1 } });
    expect(resolver.mock.calls.length).toBe(calls);
    view.dispatch({ annotations: notesChanged.of(true) });
    expect(resolver.mock.calls.length).toBe(calls + 1);
    view.dispatch({ changes: { from: 0, insert: 'Ж' } });
    expect(resolver.mock.calls.length).toBe(calls + 2);
    view.destroy();
  });
});

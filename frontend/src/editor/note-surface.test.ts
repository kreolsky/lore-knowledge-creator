/** Which non-editable views take notes, and the selection feed that shows the note-only bar. */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach } from 'vitest';
import { EditorState, type Extension } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { publicNoteSurface, takesNotesWithoutFocus } from './note-surface';
import { selectionFeedExtension } from './editor-observer';
import { useUIStore } from '../store/ui-store';

const READONLY: Extension = [EditorState.readOnly.of(true), EditorView.editable.of(false)];

function state(extensions: Extension): EditorState {
  return EditorState.create({ doc: 'Привет 😀 мир', extensions });
}

describe('takesNotesWithoutFocus', () => {
  it('an editable view takes notes through its own focus, not this path', () => {
    expect(takesNotesWithoutFocus(state([]))).toBe(false);
  });
  it("a commentator's non-editable editor takes notes", () => {
    expect(takesNotesWithoutFocus(state(EditorView.editable.of(false)))).toBe(true);
  });
  it('the snapshot preview (readOnly) never does — it shows a checkpoint', () => {
    expect(takesNotesWithoutFocus(state(READONLY))).toBe(false);
  });
  it('the public viewer (readOnly, live payload) does', () => {
    expect(takesNotesWithoutFocus(state([READONLY, publicNoteSurface.of(true)]))).toBe(true);
  });
});

describe('selection feed', () => {
  beforeEach(() => useUIStore.getState().setSelectionEmpty(true));

  function selectIn(extensions: Extension): boolean {
    const view = new EditorView({
      state: state([extensions, selectionFeedExtension]),
      parent: document.createElement('div'),
    });
    view.dispatch({ selection: { anchor: 9, head: 12 } });
    view.destroy();
    return useUIStore.getState().selectionEmpty;
  }

  it('a selection in an unfocused non-editable view is live (the note-only bar may show)', () => {
    expect(selectIn(EditorView.editable.of(false))).toBe(false);
  });
  it('a selection in an unfocused editable view is not', () => {
    expect(selectIn([])).toBe(true);
  });
  it('a selection in the snapshot preview is not', () => {
    expect(selectIn(READONLY)).toBe(true);
  });
});

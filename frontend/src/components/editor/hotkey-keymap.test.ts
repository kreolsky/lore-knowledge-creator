/** readonlyNoteHotkeys: Mod-m reaches a non-editable view; nothing else does. */
// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView, keymap } from '@codemirror/view';

const { createNote } = vi.hoisted(() => ({ createNote: vi.fn(() => true) }));
vi.mock('./markdown-actions', () => ({ markdownActionRegistry: { createNote } }));

import { buildKeymapExtension, readonlyNoteHotkeys } from './hotkey-keymap';

function mount(opts: { editable: boolean; readOnly?: boolean }) {
  const backspace = vi.fn(() => true);
  const view = new EditorView({
    state: EditorState.create({
      doc: 'Привет 😀 мир',
      selection: { anchor: 0, head: 6 },
      extensions: [
        // Ctrl, not Mod: Mod resolves per platform, the test must not.
        buildKeymapExtension({ 'Ctrl-m': 'createNote' }),
        keymap.of([{ key: 'Backspace', run: backspace }]),
        readonlyNoteHotkeys,
        EditorView.editable.of(opts.editable),
        EditorState.readOnly.of(opts.readOnly ?? false),
      ],
    }),
    parent: document.body,
  });
  const range = document.createRange();
  range.selectNodeContents(view.contentDOM);
  document.getSelection()!.removeAllRanges();
  document.getSelection()!.addRange(range);
  return { view, backspace };
}

function press(key: string, ctrl = false): void {
  window.dispatchEvent(new KeyboardEvent('keydown', { key, ctrlKey: ctrl, bubbles: true, cancelable: true }));
}

describe('readonlyNoteHotkeys', () => {
  it('runs Mod-m for a non-editable view holding the selection, and only the note scope', () => {
    createNote.mockClear();
    const { view, backspace } = mount({ editable: false });
    press('m', true);
    expect(createNote).toHaveBeenCalledTimes(1);
    press('Backspace');
    expect(backspace).not.toHaveBeenCalled();
    view.destroy();
  });

  it('stays out of an editable view (its own keymap owns the key) and a readOnly preview', () => {
    createNote.mockClear();
    const editable = mount({ editable: true });
    press('m', true);
    editable.view.destroy();
    const preview = mount({ editable: false, readOnly: true });
    press('m', true);
    preview.view.destroy();
    expect(createNote).not.toHaveBeenCalled();
  });

  it('stops listening once the view is destroyed', () => {
    createNote.mockClear();
    const { view } = mount({ editable: false });
    view.destroy();
    press('m', true);
    expect(createNote).not.toHaveBeenCalled();
  });
});

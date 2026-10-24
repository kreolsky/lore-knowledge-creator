/** Tests for CheckboxWidget — a click toggles the task marker only in an editable view. */

// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import { EditorState, type Extension } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { CheckboxWidget } from './widgets';

function clickCheckbox(extensions: Extension[]): string {
  const view = new EditorView({ state: EditorState.create({ doc: '- [ ] task', extensions }) });
  const input = new CheckboxWidget(false, 2).toDOM(view);
  input.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
  const doc = view.state.doc.toString();
  view.destroy();
  return doc;
}

describe('CheckboxWidget', () => {
  it('toggles the marker in an editable view', () => {
    expect(clickCheckbox([EditorView.editable.of(true)])).toBe('- [x] task');
  });

  it('refuses the toggle when the view is not editable (viewer / offline)', () => {
    expect(clickCheckbox([EditorView.editable.of(false)])).toBe('- [ ] task');
  });

  it('refuses the toggle when the state is readOnly (preview / transclusion)', () => {
    expect(clickCheckbox([EditorState.readOnly.of(true)])).toBe('- [ ] task');
  });
});

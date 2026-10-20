/** Which non-editable views take notes: the one rule the selection feed and Mod-m share. */
import { Facet, type EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';

/** Set by the public viewer: its state is readOnly, yet it shows the live text. */
export const publicNoteSurface = Facet.define<boolean, boolean>({
  combine: values => values.some(Boolean),
});

/**
 * A view the user cannot type into that still takes notes on its selection: a
 * commentator's editor, or the public viewer. Never the snapshot preview — its
 * readOnly state shows a checkpoint, not the live text an anchor is built on.
 */
export function takesNotesWithoutFocus(state: EditorState): boolean {
  if (state.facet(EditorView.editable)) return false;
  return !state.readOnly || state.facet(publicNoteSurface);
}

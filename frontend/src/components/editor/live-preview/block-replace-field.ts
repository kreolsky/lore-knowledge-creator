/**
 * Generic StateField factory for multi-line Decoration.replace blocks.
 *
 * ARCH: CM6 forbids Decoration.replace that spans multiple lines inside a
 * ViewPlugin (line-break restriction), so block-replace decorations MUST live
 * in a StateField. Several live-preview block types (table, table-block, math,
 * mermaid) share the same `update()` shape: rebuild on `docChanged` or
 * tree-change; when `cursorSensitive`, additionally rebuild when the cursor
 * enters/leaves any tracked bound (so the raw markdown can be revealed while
 * the caret is inside, then hidden again). This factory centralises that shape.
 *
 * Shape contract (PINNED): the field's
 * state type is UNIFORMLY `{ decs: DecorationSet; bounds: { from, to }[] }`.
 * `build` ALWAYS returns this shape. For `cursorSensitive:false` fields the
 * `bounds` array is returned empty (`[]`) and the selection branch is skipped —
 * the array exists but is unused. The factory does NOT accept a plain
 * DecorationSet return; that would fork the state type and re-introduce the
 * duplication the factory exists to remove. Callers that ignore bounds
 * (tableBlock) simply never read them.
 */
// SYSTEM: block-replace-field — shared StateField factory for multi-line block decorations

import { syntaxTree } from '@codemirror/language';
import { Decoration, DecorationSet, EditorView } from '@codemirror/view';
import { EditorState, StateField } from '@codemirror/state';

export interface BlockReplaceState {
  decs: DecorationSet;
  bounds: { from: number; to: number }[];
}

export interface BlockReplaceBuild {
  (state: EditorState): BlockReplaceState;
}

export interface BlockReplaceOptions {
  cursorSensitive: boolean;
  /**
   * Expose the field's block ranges via `EditorView.atomicRanges`. Only for
   * ALWAYS-widget blocks whose raw text is never shown (table anchors): motion
   * commands skip the hidden range as one unit and a delete that reaches it takes
   * the whole atom. cursorSensitive fields (GFM table / math / mermaid) reveal raw
   * markdown under the caret, so the caret MUST be able to enter them — they never
   * set this.
   */
  atomic?: boolean;
}

/**
 * Build a StateField whose decorations are multi-line Decoration.replace blocks.
 *
 * `build` returns `{ decs, bounds }`. When `cursorSensitive` is true the field
 * also rebuilds on cursor enter/leave of any bound (so raw markdown can be
 * revealed while the caret is inside the block). When false the bounds are
 * `[]` and the selection branch never fires.
 */
export function blockReplaceField(
  build: BlockReplaceBuild,
  options: BlockReplaceOptions,
): StateField<BlockReplaceState> {
  const { cursorSensitive, atomic } = options;
  return StateField.define<BlockReplaceState>({
    create(state) { return build(state); },
    update(state, tr) {
      if (tr.docChanged || syntaxTree(tr.state) !== syntaxTree(tr.startState)) {
        return build(tr.state);
      }
      if (cursorSensitive && tr.selection) {
        const oldH = tr.startState.selection.main.head;
        const newH = tr.state.selection.main.head;
        const wasIn = state.bounds.some(t => oldH >= t.from && oldH <= t.to);
        const nowIn = state.bounds.some(t => newH >= t.from && newH <= t.to);
        if (wasIn || nowIn) return build(tr.state);
      }
      return state;
    },
    provide: (f) =>
      atomic
        ? [
            EditorView.decorations.from(f, v => v.decs),
            // Every range in this field is a whole-block replace of hidden anchor
            // text — all of them are atoms. CM6 atomicity is strict-containment:
            // caret stops at the range EDGES stay legal, so traversal is never
            // trapped at document boundaries.
            EditorView.atomicRanges.of(view => view.state.field(f).decs),
          ]
        : EditorView.decorations.from(f, v => v.decs),
  });
}

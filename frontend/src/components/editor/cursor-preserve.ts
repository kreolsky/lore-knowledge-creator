/**
 * Cursor mapping helper for list/indent transactions.
 *
 * ARCH: List-action commands (indent, outdent, toggleBullet/Numbered/Checkbox)
 * historically computed post-change cursor positions via ad-hoc arithmetic
 * (`from + 1`, `to + totalDelta`, `startLine.from + indentLen + markerLen`).
 * That arithmetic occasionally lands the cursor on the right boundary of a
 * `Decoration.replace` widget (e.g. CheckboxWidget) at end-of-line, where
 * browser- and parser-shape-specific snapping pulls the cursor inside the
 * widget. This helper centralises the rule so callers cannot reintroduce the
 * boundary hazard line by line.
 */
import type { ChangeSpec, EditorState, SelectionRange } from '@codemirror/state';
import { ChangeSet, EditorSelection } from '@codemirror/state';

/**
 * Map a single endpoint by preserving its distance from its line's `line.to`.
 * A cursor at end-of-line stays at end-of-line; a cursor mid-text stays the
 * same number of characters away from the line end. Lines that no longer
 * exist after the change fall back to the new doc end.
 */
function mapEndpointFromLineEnd(prev: EditorState, changes: ChangeSet, pos: number): number {
  const prevLine = prev.doc.lineAt(pos);
  const offsetFromEnd = prevLine.to - pos;
  const newDoc = changes.apply(prev.doc);
  const newLineFrom = changes.mapPos(prevLine.from, 1);
  if (newLineFrom > newDoc.length) return newDoc.length;
  const newLine = newDoc.lineAt(newLineFrom);
  return Math.max(newLine.from, newLine.to - offsetFromEnd);
}

/**
 * Build a post-change selection that preserves each endpoint's offset from
 * its line.to. Used by `indentLines` / `outdentLines` so that Tab/Cmd+] /
 * Cmd+[ never drops the cursor onto a widget boundary.
 */
export function preserveCursorFromLineEnd(
  prev: EditorState,
  changes: ChangeSpec,
  prevSel: SelectionRange,
): SelectionRange {
  const set = ChangeSet.of(changes, prev.doc.length);
  const anchor = mapEndpointFromLineEnd(prev, set, prevSel.anchor);
  const head = prevSel.empty ? anchor : mapEndpointFromLineEnd(prev, set, prevSel.head);
  return EditorSelection.range(anchor, head);
}

/**
 * Build a post-change selection that snaps to the affected line range.
 * Used by `toggleBulletList` / `toggleNumberedList` / `toggleCheckboxList`:
 *   - empty selection → cursor at endLine.to (post-change)
 *   - non-empty selection → range (startLine.from .. endLine.to) (post-change)
 *
 * The line numbers are taken from the PRE-change state; the helper resolves
 * them in the new doc via `changes.mapPos` on each line's start.
 */
export function snapSelectionToLineEnd(
  prev: EditorState,
  changes: ChangeSpec,
  startLineNumber: number,
  endLineNumber: number,
  emptySelection: boolean,
): SelectionRange {
  const set = ChangeSet.of(changes, prev.doc.length);
  const newDoc = set.apply(prev.doc);
  const prevStart = prev.doc.line(startLineNumber);
  const prevEnd = prev.doc.line(endLineNumber);
  const newStartFrom = Math.min(set.mapPos(prevStart.from, 1), newDoc.length);
  const newEndFrom = Math.min(set.mapPos(prevEnd.from, 1), newDoc.length);
  const endLineTo = newDoc.lineAt(newEndFrom).to;
  if (emptySelection) {
    return EditorSelection.cursor(endLineTo);
  }
  return EditorSelection.range(newDoc.lineAt(newStartFrom).from, endLineTo);
}

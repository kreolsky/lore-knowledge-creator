/** Width of an editor's document text column, the target a table fit fills. */
import type { EditorView } from '@codemirror/view';

/**
 * The text column's width in px: `.cm-content` minus its own and a `.cm-line`'s side
 * padding. WHY the -1: a frozen table box is `sum + 1` wide (collapsed outer border, see
 * `applyColumnWidths` in table-block-widget.ts). Null when the view is not laid out.
 */
export function documentTextWidth(view: EditorView): number | null {
  const content = view.contentDOM;
  const line = content.querySelector('.cm-line');
  const pad = (el: Element) => {
    const style = getComputedStyle(el);
    return parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
  };
  const width = content.clientWidth - pad(content) - (line ? pad(line) : 0) - 1;
  return width > 0 ? Math.floor(width) : null;
}

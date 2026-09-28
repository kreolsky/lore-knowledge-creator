/**
 * CM6 extension: rich clipboard copy/cut (HTML + portable markdown).
 *
 * Cmd+C / Cmd+X → writes BOTH `text/html` (rendered, class-free semantic HTML via
 * `cleanMarkdownToHtml`) and a portable `text/plain` (markdown source, with table
 * transclusion anchors expanded to GFM). External rich editors (Word / Google Docs /
 * Gmail / Notion) read `text/html` and preserve formatting; plain targets (Notepad, code
 * editors) read `text/plain`. Paste already converts HTML→Markdown (see paste-handler).
 *
 * ARCH: EditorView.domEventHandlers intercept copy/cut BEFORE CM6's default handler. The
 * selection's raw markdown source is the single portable intermediate: table anchors are
 * expanded to GFM (an anchor is meaningless outside the app) and BOTH clipboard payloads
 * derive from that one string. The copy path is READ-ONLY on the Y.Doc (table model
 * helpers never mutate here); it degrades gracefully — a null active handle (mount race)
 * or a missing table model leaves anchors as-is and never throws.
 * SYSTEM: editor — clipboard copy handler
 */

import { EditorView } from '@codemirror/view';
import { getActiveHandle } from './active-editor';
import { cleanMarkdownToHtml } from '../components/editor/markdown-to-html';
import { expandTableAnchorsToGfm } from '../components/editor/live-preview/table-block-model';
import { ligatureSubstitutedText } from '../components/editor/ligature-plugin';

// CF_HTML-compatible envelope. The browser serializes this `text/html` to the native
// clipboard (computing the StartHTML/EndHTML offsets itself); the StartFragment/EndFragment
// comments are what Word/Outlook use to delimit the pasted fragment.
const FRAGMENT_HEAD = '<html>\n<head><meta charset="utf-8"></head>\n<body>\n';
const FRAGMENT_TAIL = '\n</body>\n</html>';

// Private sentinel embedded in the `text/html` payload. The paste handler detects it and
// opts into the lossless `text/plain` source instead of round-tripping through Turndown.
// Why an HTML comment: invisible to external editors and works in the standard clipboard
// channel without relying on custom MIME types.
const LORE_CLIP_MARK = '<!--lore-clipboard-->';

/**
 * Write the rich clipboard payload for a copy/cut event. Returns true when handled (the
 * default copy/cut is suppressed), false to let CM6's default run (no clipboardData, or an
 * all-collapsed selection). `isCut` additionally deletes every selection range.
 */
export function handleCopyCut(e: ClipboardEvent, view: EditorView, isCut: boolean): boolean {
  if (!e.clipboardData) return false;
  const { selection } = view.state;
  // All-collapsed selection → nothing selected; let CM6 default copy the current line.
  if (selection.ranges.every((r) => r.from === r.to)) return false;

  // Multi-range join uses a single `\n` to match CM6's default copy semantics.
  const ranges = selection.ranges;
  const selectedMarkdown = ranges
    .map((r) => view.state.sliceDoc(r.from, r.to))
    .join('\n');
  // Glyph-substituted markdown for the text/html payload ONLY. text/plain keeps the
  // lossless source (LORE_CLIP_MARK Lore→Lore paste reads text/plain → no doc mutation).
  const renderedMarkdown = ranges
    .map((r) => ligatureSubstitutedText(view.state, r.from, r.to))
    .join('\n');

  // Expand table anchors to portable GFM when the live collab handle is available; on a
  // mount race (no handle) keep anchors verbatim — never throw out of the copy path.
  const handle = getActiveHandle();
  const sourcePortable = handle?.ydoc
    ? expandTableAnchorsToGfm(handle.ydoc, selectedMarkdown)
    : selectedMarkdown;
  // INVARIANT: ligature substitution runs on the SOURCE slice (tables still anchors),
  // THEN anchors expand — never the reverse.
  // Why: reversing it would substitute the `---` in a produced GFM delimiter row
  // (`| --- | --- |`) to `—` and corrupt the table (copy-handler.test.ts table case).
  const renderedPortable = handle?.ydoc
    ? expandTableAnchorsToGfm(handle.ydoc, renderedMarkdown)
    : renderedMarkdown;

  const html = cleanMarkdownToHtml(renderedPortable);
  const envelope =
    FRAGMENT_HEAD +
    '<!--StartFragment-->' +
    LORE_CLIP_MARK +
    html +
    '<!--EndFragment-->' +
    FRAGMENT_TAIL;

  e.preventDefault();
  e.clipboardData.setData('text/plain', sourcePortable);
  e.clipboardData.setData('text/html', envelope);

  if (isCut) {
    // Delete every selection range and collapse the cursor to the primary range's
    // start, mapped through the deletions so an earlier-deleted range shifts the
    // anchor left. CM6 stores an explicitly-provided selection AS-IS in post-change
    // coords (see @codemirror/state Transaction.selection getter), so selection.main.from
    // (pre-change) must be mapped manually — never relied on as the new anchor.
    const deletes = selection.ranges.map((r) => ({ from: r.from, to: r.to }));
    view.dispatch({
      changes: deletes,
      selection: { anchor: view.state.changes(deletes).mapPos(selection.main.from) },
    });
  }
  return true;
}

export const copyHandlerExtension = EditorView.domEventHandlers({
  copy: (e, view) => handleCopyCut(e, view, false),
  cut: (e, view) => handleCopyCut(e, view, true),
});

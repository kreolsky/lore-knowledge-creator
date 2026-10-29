/**
 * CM6 extension: rich clipboard copy/cut (HTML + portable markdown).
 *
 * Cmd+C / Cmd+X → writes BOTH `text/html` (rendered, class-free semantic HTML via
 * `cleanMarkdownToHtml`) and a portable `text/plain` (markdown source, with table
 * transclusion anchors expanded to GFM). External rich editors (Word / Google Docs /
 * Gmail / Notion) read `text/html` and preserve formatting; plain targets (Notepad, code
 * editors) read `text/plain`. A whole-selected image also puts the picture itself
 * (`image/png`) on the clipboard. Paste already converts HTML→Markdown (see paste-handler).
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
import { selectedAtomicRange } from '../components/editor/live-preview/atomic-delete-guard';
import { useAppStore } from '../store/app-store';
import { t } from '../i18n';

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

/** The rendered `<img>` of the whole-selected image embed, or null for any other selection. */
function selectedImageElement(view: EditorView): HTMLImageElement | null {
  const range = selectedAtomicRange(view.state);
  if (!range || range.tableId !== null) return null;
  for (const img of view.contentDOM.querySelectorAll<HTMLImageElement>('img:not(.cm-widgetBuffer)')) {
    let pos: number;
    try { pos = view.posAtDOM(img); } catch { continue; }
    if (pos >= range.from && pos < range.to) return img;
  }
  return null;
}

/** Re-encode a loaded image as PNG — the only image flavor browsers accept on the clipboard. */
function imageToPng(img: HTMLImageElement): Promise<Blob> {
  // WHY inside the executor: a cross-origin (tainted) canvas makes toBlob THROW synchronously;
  // the executor turns that into a rejection instead of an exception out of the copy handler.
  return new Promise((resolve, reject) => {
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('no 2d context');
    ctx.drawImage(img, 0, 0);
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('toBlob returned null'))), 'image/png');
  });
}

/**
 * text/html for a copied image: the picture inlined as a data URI, plus the Lore marker.
 * WHY: rich targets (Google Docs, Word) read text/html BEFORE image/png — the rendered
 * markdown of an embed is just its filename text. A data URI needs no auth to fetch.
 */
function imageHtml(png: Blob): Promise<Blob> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const body = `<!--StartFragment-->${LORE_CLIP_MARK}<img src="${reader.result as string}"><!--EndFragment-->`;
      resolve(new Blob([FRAGMENT_HEAD + body + FRAGMENT_TAIL], { type: 'text/html' }));
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(png);
  });
}

/**
 * Whole-selected image → additionally put the picture itself (`image/png`) on the clipboard,
 * next to the same markdown/html flavors, so external apps paste the media.
 * WHY async Clipboard API: `clipboardData.setData` takes strings only. The ClipboardItem is
 * built synchronously inside the gesture with a PROMISE for the PNG — Safari rejects a
 * write that starts after an await. The Lore marker stays in text/html, so a Lore paste
 * still resolves to the markdown link, never a re-upload. On failure the user is told
 * the image was not copied.
 */
function writeSelectedImage(view: EditorView, img: HTMLImageElement): void {
  const { from, to } = view.state.selection.main;
  const plain = view.state.sliceDoc(from, to);
  // WHY a toast, not a silent return: the async Clipboard API exists only in a secure
  // context (https/localhost); over plain http the image would silently not be copied.
  if (typeof ClipboardItem === 'undefined' || !navigator.clipboard?.write) {
    useAppStore.getState().showToast(t('failedToCopyImage'), 'error');
    return;
  }
  const png = imageToPng(img);
  const item = new ClipboardItem({
    'image/png': png,
    'text/plain': new Blob([plain], { type: 'text/plain' }),
    'text/html': png.then(imageHtml),
  });
  navigator.clipboard.write([item]).catch((err: unknown) => {
    console.warn('[copy] image clipboard write failed', err);
    useAppStore.getState().showToast(t('failedToCopyImage'), 'error');
  });
}

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

/**
 * Cmd/Ctrl+C on a whole-selected image → write the image clipboard payload and swallow the
 * key, so no `copy` event follows. Returns false for any other key or selection.
 * WHY keydown, not the `copy` event: Firefox refuses `navigator.clipboard.write` from
 * inside a clipboard event ("Clipboard write is not allowed"); the keydown carries the
 * user activation every browser accepts. Menu Copy still goes through `handleCopyCut`
 * and copies the markdown only.
 */
function handleImageCopyKey(e: KeyboardEvent, view: EditorView): boolean {
  if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey || e.code !== 'KeyC') return false;
  const img = selectedImageElement(view);
  if (!img) return false;
  e.preventDefault();
  writeSelectedImage(view, img);
  return true;
}

export const copyHandlerExtension = EditorView.domEventHandlers({
  keydown: (e, view) => handleImageCopyKey(e, view),
  copy: (e, view) => handleCopyCut(e, view, false),
  cut: (e, view) => handleCopyCut(e, view, true),
});

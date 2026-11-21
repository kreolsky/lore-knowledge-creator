/**
 * CM6 extension: rich clipboard copy/cut (HTML + portable markdown).
 *
 * Cmd+C / Cmd+X → writes BOTH `text/html` (rendered, class-free semantic HTML via
 * `cleanMarkdownToHtml`, each table rendered from its model with merged cells as
 * `colspan`/`rowspan`) and a portable `text/plain` (markdown source, with table
 * transclusion anchors expanded to GFM). External rich editors (Word / Google Docs /
 * Gmail / Notion) read `text/html` and preserve formatting; plain targets (Notepad, code
 * editors) read `text/plain`. A whole-selected image also puts the picture itself
 * (`image/png`) on the clipboard; Cmd/Ctrl+C on a selection with `ref:` images inlines
 * them into `text/html` as data URIs. Paste already converts HTML→Markdown (see paste-handler).
 *
 * ARCH: EditorView.domEventHandlers intercept copy/cut BEFORE CM6's default handler. The
 * selection's raw markdown source is the single portable intermediate: `text/plain` expands
 * table anchors to GFM (an anchor is meaningless outside the app); `text/html` renders each
 * anchor straight from the table model as an HTML island, since GFM has no merged cells.
 * The copy path is READ-ONLY on the Y.Doc (table model helpers never mutate here); it degrades gracefully — a null active handle (mount race)
 * or a missing table model leaves anchors as-is and never throws.
 * SYSTEM: editor — clipboard copy handler
 */

import { EditorView } from '@codemirror/view';
import { getActiveHandle } from './active-editor';
import { cleanMarkdownToHtml, isInlinableDataUri } from '../components/editor/markdown-to-html';
import { expandTableAnchorsToGfm, tableAnchorsToIslands, tableMergesByGfm } from '../components/editor/live-preview/table-serialize';
import { ligatureSubstitutedText } from '../components/editor/ligature-plugin';
import { selectedAtomicRange } from '../components/editor/live-preview/atomic-delete-guard';
import { cutNotesSidecar } from './note-clipboard';
import { transcludeMap } from '../components/editor/live-preview/effects';
import { splitTarget } from '../components/editor/live-preview/link-types';
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
// Merged areas of the copied tables (`[gfm, areas][]`, URI-encoded JSON — `-->` cannot occur
// in it). Read back by the paste handler's Lore→Lore path; external editors ignore comments.
const LORE_MERGES_PREFIX = '<!--lore-table-merges:';

/** The rendered `<img>` of the whole-selected image embed, or null for any other selection. */
function selectedImageElement(view: EditorView): HTMLImageElement | null {
  const range = selectedAtomicRange(view.state);
  if (range?.kind !== 'image') return null;
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

/** A copy payload: `text/plain` now, `text/html` rendered on demand with inlined images. */
interface CopyPayload {
  plain: string;
  renderHtml(images?: ReadonlyMap<string, string>): string;
}

/**
 * Snapshot the copy payload of the current (non-collapsed) selection. Everything is read
 * from the editor NOW; only the final markdown→HTML render is deferred, so the keydown path
 * can render after its image fetches without seeing edits made meanwhile.
 */
function buildCopyPayload(view: EditorView, isCut: boolean): CopyPayload {
  // Multi-range join uses a single `\n` to match CM6's default copy semantics.
  const ranges = view.state.selection.ranges;
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
  const rendered = handle?.ydoc
    ? tableAnchorsToIslands(handle.ydoc, renderedMarkdown)
    : { text: renderedMarkdown, islands: [] };

  // WHY from the SOURCE slice: the paste side matches `text/plain` (source GFM), and the
  // rendered slice's ligatures would never match it.
  const merges = handle?.ydoc ? tableMergesByGfm(handle.ydoc, selectedMarkdown) : [];
  const mergesSidecar = merges.length ? `${LORE_MERGES_PREFIX}${encodeURIComponent(JSON.stringify(merges))}-->` : '';
  // WHY cut only and single range only: a copy orphans nothing, and a multi-range cut's
  // pasted text is the ranges joined, which no single offset base describes.
  const notesSidecar = isCut && ranges.length === 1 ? cutNotesSidecar(view, ranges[0].from, ranges[0].to) : '';
  const renderHtml = (images?: ReadonlyMap<string, string>): string =>
    FRAGMENT_HEAD +
    '<!--StartFragment-->' +
    LORE_CLIP_MARK +
    mergesSidecar +
    notesSidecar +
    cleanMarkdownToHtml(rendered.text, rendered.islands, images) +
    '<!--EndFragment-->' +
    FRAGMENT_TAIL;
  return { plain: sourcePortable, renderHtml };
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

  const payload = buildCopyPayload(view, isCut);
  e.preventDefault();
  e.clipboardData.setData('text/plain', payload.plain);
  e.clipboardData.setData('text/html', payload.renderHtml());

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

// A `ref:` image embed in the source; group 1 = the target as written (`ref:<id>[#frag]`).
const REF_IMAGE_RE = /!\[[^\]]*\]\((ref:[^)\s]+)\)/g;
// WHY: a fenced block renders as literal code, so an embed inside it is never an <img>.
const FENCED_BLOCK_RE = /```[\s\S]*?```/g;
// WHY a cap: the clipboard holds the payload as a string ~1.33x the bytes, and Google Docs
// drops an oversized paste silently.
const MAX_INLINE_BYTES = 25 * 1024 * 1024;
const FETCH_CONCURRENCY = 4;

/** Image target as written → its loaded image URL, for every resolvable `ref:` image in the selection. */
function collectSelectedImages(view: EditorView): Map<string, string> {
  const urls = new Map<string, string>();
  for (const r of view.state.selection.ranges) {
    for (const m of view.state.sliceDoc(r.from, r.to).replace(FENCED_BLOCK_RE, '').matchAll(REF_IMAGE_RE)) {
      const entry = transcludeMap.get(splitTarget(m[1].slice('ref:'.length)).id);
      if (entry?.kind === 'ref-image' && entry.imageUrl) urls.set(m[1], entry.imageUrl);
    }
  }
  return urls;
}

function blobToDataUri(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

/**
 * Fetch each image ORIGINAL (already in the HTTP cache from the editor render) as a data URI.
 * Never rejects: an image that fails, is not a raster, or exceeds the size cap is counted in
 * `missed` and stays alt text. WHY no canvas: keeps the original format and cannot taint.
 */
async function inlineImages(urls: ReadonlyMap<string, string>): Promise<{ images: Map<string, string>; missed: number }> {
  const queue = [...urls];
  const images = new Map<string, string>();
  let missed = 0;
  let bytes = 0;
  const worker = async (): Promise<void> => {
    for (let next = queue.shift(); next; next = queue.shift()) {
      const [target, url] = next;
      try {
        const res = await fetch(url, { cache: 'force-cache' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const blob = await res.blob();
        if (bytes + blob.size > MAX_INLINE_BYTES) throw new Error('clipboard size cap reached');
        const dataUri = await blobToDataUri(blob);
        // WHY the pipeline's own predicate: a URI it would refuse (svg, a typed-with-params
        // blob) must count as missed here, or the image turns to text with no toast.
        if (!isInlinableDataUri(dataUri)) throw new Error(`not an inlinable image: ${blob.type}`);
        bytes += blob.size;
        images.set(target, dataUri);
      } catch (err) {
        console.warn('[copy] image not inlined', url, err);
        missed += 1;
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(FETCH_CONCURRENCY, queue.length) }, worker));
  return { images, missed };
}

/**
 * Cmd/Ctrl+C on a selection containing `ref:` images → write `text/html` with each image
 * inlined as a data URI, so Google Docs / Word paste the pictures. Returns false (the
 * ordinary `copy` event runs) for a selection with no resolvable image.
 * WHY keydown + a ClipboardItem of PROMISES built inside the gesture: same constraints as
 * `handleImageCopyKey` — Firefox refuses `clipboard.write` inside a `copy` event and Safari
 * rejects a write started after an await. `text/plain` is the same source markdown as the
 * event path, so a Lore→Lore paste is unchanged. Cut and the menu Copy stay on the event
 * path, text-only: cut must delete synchronously and the menu carries no keydown.
 */
function handleRichCopyKey(e: KeyboardEvent, view: EditorView): boolean {
  if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey || e.code !== 'KeyC') return false;
  if (view.state.selection.ranges.every((r) => r.from === r.to)) return false;
  const urls = collectSelectedImages(view);
  if (urls.size === 0) return false;
  const { showToast } = useAppStore.getState();
  // WHY fall through to the event path: no async Clipboard API (plain http) — the text
  // still copies, and the user is told the images did not.
  if (typeof ClipboardItem === 'undefined' || !navigator.clipboard?.write) {
    showToast(t('someImagesNotCopied'), 'error');
    return false;
  }
  e.preventDefault();
  const payload = buildCopyPayload(view, false);
  const inlined = inlineImages(urls);
  const item = new ClipboardItem({
    'text/plain': new Blob([payload.plain], { type: 'text/plain' }),
    'text/html': inlined.then(({ images }) => new Blob([payload.renderHtml(images)], { type: 'text/html' })),
  });
  navigator.clipboard.write([item])
    .then(() => inlined)
    .then(({ missed }) => { if (missed > 0) showToast(t('someImagesNotCopied'), 'error'); })
    .catch((err: unknown) => {
      console.warn('[copy] rich clipboard write failed', err);
      showToast(t('copyFailed'), 'error');
    });
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
  keydown: (e, view) => handleImageCopyKey(e, view) || handleRichCopyKey(e, view),
  copy: (e, view) => handleCopyCut(e, view, false),
  cut: (e, view) => handleCopyCut(e, view, true),
});

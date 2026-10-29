/**
 * Widget classes for live-preview: checkboxes, bullets, images, copy buttons, math.
 */
import { EditorView, WidgetType } from '@codemirror/view';
import { EditorSelection, EditorState } from '@codemirror/state';
import { renderMathCached } from '../math-render';
import { renderMermaid } from '../../../editor/mermaid-render';
import { useAppStore } from '../../../store/app-store';
import { emit } from '../../../events';
import { nestedRenderExtensions } from '../render-bundle';
import { copyWithToast } from '../../chat/shared/copy';
import { COPY_ICON, CHECK_ICON } from '../../../utils/copy-icons';
import { formatFileSize } from '../../references/ref-utils';
import type { TransclusionEntry } from './effects';
// WHY direct import despite the module cycle (widgets → atomic-delete-guard →
// fields → widgets): atomicRangeAt is only called INSIDE function bodies, never
// at module-eval time, so the cycle resolves the same way as the documented
// live-preview one.
import { atomicRangeAt } from './atomic-delete-guard';

// ─── Checkbox widget ──────────────────────────────────────────────────────────

/**
 * Renders a task-list checkbox. Clicking it dispatches a CM transaction that
 * toggles `[ ]` ↔ `[x]` at the TaskMarker's document position.
 * ignoreEvent returns true so CM never moves the cursor on widget clicks.
 */
export class CheckboxWidget extends WidgetType {
  constructor(
    readonly checked: boolean,
    /** Absolute doc position of the TaskMarker `[` character. */
    readonly markerFrom: number,
  ) { super(); }

  toDOM(view: EditorView): HTMLElement {
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = this.checked;
    input.className = 'cm-task-checkbox';

    input.addEventListener('click', (e) => {
      e.preventDefault();
      // INVARIANT: a task checkbox toggles only in an editable view — a viewer,
      // an offline/binding editor and a readOnly preview never change the doc.
      // Why: view.dispatch bypasses both facets (they gate DOM input, not
      // programmatic transactions), so the widget must check them itself; the
      // editable compartment is reconfigured live, hence the check at click time.
      if (view.state.readOnly || !view.state.facet(EditorView.editable)) return;
      const current = view.state.doc.sliceString(this.markerFrom, this.markerFrom + 3);
      const isChecked = current === '[x]' || current === '[X]';
      view.dispatch({
        changes: { from: this.markerFrom, to: this.markerFrom + 3, insert: isChecked ? '[ ]' : '[x]' },
      });
    });

    return input;
  }

  eq(other: CheckboxWidget): boolean {
    return other.checked === this.checked && other.markerFrom === this.markerFrom;
  }

  /** Prevent CM from moving cursor or processing pointer events on the widget. */
  ignoreEvent(): boolean { return true; }
}

// ─── Bullet widget ───────────────────────────────────────────────────────────

export class BulletWidget extends WidgetType {
  toDOM(): HTMLElement {
    const span = document.createElement('span');
    span.textContent = '•';
    span.className = 'cm-bullet';
    span.style.cssText = 'margin-right:0.2em;font-size:1.5em;line-height:0;vertical-align:-0.04em;';
    return span;
  }

  eq(): boolean { return true; }
  ignoreEvent(): boolean { return false; }
}

// ─── Image sizing ─────────────────────────────────────────────────────────────

/** The size-suffix grammar shared by parseSizeFromAlt, setImageSize and the ImageToolbar's size field. */
export const SIZE_SUFFIX_RE = /^(\d+%?)(?:x(\d+%?))?$/;

/** Parses `|WIDTHxHEIGHT` from the alt text of an image node. */
export function parseSizeFromAlt(alt: string): { width?: string; height?: string } {
  const pipeIdx = alt.lastIndexOf('|');
  if (pipeIdx === -1) return {};
  const m = SIZE_SUFFIX_RE.exec(alt.slice(pipeIdx + 1).trim());
  if (!m) return {};
  const toCSS = (v: string) => v.endsWith('%') ? v : `${v}px`;
  return { width: toCSS(m[1]), height: m[2] ? toCSS(m[2]) : undefined };
}

/**
 * Splits a VALID size suffix off an image alt: `{ base, height }`. A `|` whose
 * remainder does not parse as `WIDTH[xHEIGHT]` belongs to the base (mirrors
 * parseSizeFromAlt, so the two never disagree on where the suffix starts).
 */
function splitSizeSuffix(alt: string): { base: string; height?: string } {
  const pipeIdx = alt.lastIndexOf('|');
  if (pipeIdx === -1) return { base: alt };
  const m = SIZE_SUFFIX_RE.exec(alt.slice(pipeIdx + 1).trim());
  if (!m) return { base: alt };
  return { base: alt.slice(0, pipeIdx), height: m[2] };
}

/**
 * The writer half of parseSizeFromAlt: rewrites ONLY the width of an image
 * alt's `|…` size suffix. `300` ⇒ px, `50%` ⇒ percent; `''` clears the suffix
 * WHOLE (width and height); an authored `x<height>` is preserved verbatim; an
 * invalid value leaves the alt unchanged. Writers never touch any other part
 * of the embed text — no new syntax, only the existing suffix.
 */
export function setImageSize(alt: string, value: string): string {
  const { base, height } = splitSizeSuffix(alt);
  if (value === '') return base;
  if (!/^\d+%?$/.test(value)) return alt;
  return `${base}|${height ? `${value}x${height}` : value}`;
}

/** The width an image may be drawn at inside `el`: its client width minus horizontal
 * padding (the document's `.cm-line` keeps CM6's side padding; a cell's is zero).
 * 0 when `el` has no layout. */
export function contentBoxWidth(el: HTMLElement): number {
  const cs = getComputedStyle(el);
  return el.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
}

/** alt + url of a raw `![alt](url)` embed text, or null for non-embed text. */
export function imageEmbedParts(raw: string): { alt: string; url: string } | null {
  const m = /^!\[([\s\S]*)\]\(([^)]*)\)$/.exec(raw);
  return m ? { alt: m[1], url: m[2] } : null;
}

/**
 * Applies a new size value to the image embed covering [from, to] in ONE
 * transaction, re-selecting the new node range so the image toolbar stays up
 * across its own edit (the atomic block remains selected whole). Returns false
 * (no dispatch) when the range is not a `![alt](url)` embed or the value
 * changed nothing. Shared by the ImageToolbar's size field and the widget's
 * resize handle — the two writers of the suffix.
 */
export function applyImageSize(view: EditorView, from: number, to: number, value: string): boolean {
  return rewriteImageAlt(view, from, to, (alt) => setImageSize(alt, value));
}

/**
 * Replaces the WHOLE size suffix (`300` or `300x200`) — the resize handles'
 * writer: the corner drops an authored height, the edges set both axes.
 */
export function applyImageSuffix(view: EditorView, from: number, to: number, suffix: string): boolean {
  if (!SIZE_SUFFIX_RE.test(suffix)) return false;
  return rewriteImageAlt(view, from, to, (alt) => `${splitSizeSuffix(alt).base}|${suffix}`);
}

function rewriteImageAlt(
  view: EditorView, from: number, to: number, rewrite: (alt: string) => string,
): boolean {
  const parts = imageEmbedParts(view.state.doc.sliceString(from, to));
  if (!parts) return false;
  const nextAlt = rewrite(parts.alt);
  if (nextAlt === parts.alt) return false;
  const insert = `![${nextAlt}](${parts.url})`;
  view.dispatch({
    changes: { from, to, insert },
    selection: { anchor: from, head: from + insert.length },
  });
  return true;
}

// ARCH: Cache actual rendered heights so CM6 height estimates match reality after first
// load, preventing scroll jumps on decoration rebuilds. UNIFIED across widget types — the
// composite key never collides (image src vs transclusion entity id).
export const widgetHeightCache = new Map<string, number>();

/** Clears ALL cached widget heights (project switch — no cross-project leak). */
export function clearWidgetHeightCache(): void {
  widgetHeightCache.clear();
}

// ─── Image widget ─────────────────────────────────────────────────────────────

/**
 * Focuses the view and selects exactly the image node rendered by `el`.
 */
export function selectImageAt(view: EditorView, el: Node): void {
  view.focus();
  let pos: number;
  try { pos = view.posAtDOM(el); } catch { return; }
  // posAtDOM yields -1 for a node outside the editor content.
  if (pos < 0) return;
  // Images only (tableId === null): guardedAtomicRanges also yields table anchors.
  const atom = atomicRangeAt(view.state, pos);
  if (atom && atom.tableId === null) {
    view.dispatch({ selection: EditorSelection.range(atom.from, atom.to) });
  }
}

type ResizeDir = 'corner' | 'corner-top' | 'right' | 'bottom';
const RESIZE_DIRS: readonly ResizeDir[] = ['corner', 'corner-top', 'right', 'bottom'];
/** Corner handles resize proportionally (width only, height dropped). */
const isCorner = (dir: ResizeDir) => dir === 'corner' || dir === 'corner-top';

/** Sizes the embed box: the wrapper carries width/height, the img fills it. */
function setImageBox(wrap: HTMLElement, img: HTMLImageElement, width?: string, height?: string): void {
  wrap.style.width = width ?? '';
  wrap.style.height = height ?? '';
  // WHY fill (not contain): an explicit WxH is a deliberate non-proportional size.
  img.style.cssText = 'display:block;max-width:100%;'
    + (width ? 'width:100%;' : '')
    + (height ? 'height:100%;object-fit:fill;' : '');
}

/**
 * Replaces a ![alt](ref:id) image node with an <img> element.
 * Supports optional width/height from alt-text sizing syntax.
 *
 * ATOMIC BLOCK: the raw `![alt](ref:id)` text is never revealed (build-structural
 * renders this widget under every caret position). A plain press selects exactly
 * the node's range — the selection Delete then removes (atomic-delete-guard.ts
 * swallows one-key deletes) and the selection the step-2 toolbar keys on.
 */
export class ImageWidget extends WidgetType {
  constructor(
    readonly src: string,
    readonly width?: string,
    readonly height?: string,
  ) { super(); }

  toDOM(view: EditorView): HTMLElement {
    const wrap = document.createElement('span');
    wrap.className = 'cm-image-embed';
    const img = document.createElement('img');
    img.src = this.src;
    // WHY the size sits on the wrapper, not the <img>: a percent width on the img
    // resolves against the shrink-to-fit wrapper, which then grows to the image's
    // natural width — the box (and its handles) no longer match the picture.
    setImageBox(wrap, img, this.width, this.height);
    img.onload = () => { widgetHeightCache.set(this.src, img.offsetHeight); };
    // Select the whole node on press: preventDefault keeps the browser (and CM6 —
    // via stopPropagation) from placing a caret inside the hidden embed text; the
    // explicit view.focus() preserves what the prevented default did for us.
    img.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      selectImageAt(view, wrap);
    });
    wrap.appendChild(img);
    // WHY always mounted: the view is usually NOT editable yet at first render
    // (collab joining) and eq() never rebuilds the widget when it becomes so.
    // CSS hides the handles on a non-editable content DOM; drag time re-checks.
    for (const dir of RESIZE_DIRS) wrap.appendChild(this.buildResizeHandle(view, wrap, img, dir));
    return wrap;
  }

  /**
   * Drag-to-resize: the drag updates the box live as CSS ONLY (no transactions
   * mid-drag); pointerup writes the size suffix once (one transaction).
   * corners = proportional (`|W`, height dropped); right/bottom = free (`|WxH`).
   * pointercancel restores the pre-drag CSS.
   */
  private buildResizeHandle(
    view: EditorView, wrap: HTMLElement, img: HTMLImageElement, dir: ResizeDir,
  ): HTMLElement {
    const handle = document.createElement('span');
    handle.className = `cm-image-resize-handle cm-image-resize-${dir}`;
    handle.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      // Same live re-check as CheckboxWidget's click: a stale handle must never
      // edit a read-only view.
      if (view.state.readOnly || !view.state.facet(EditorView.editable)) return;
      e.preventDefault();
      e.stopPropagation();
      // Select the image first: the text caret disappears (a non-empty
      // selection draws none) and the resize happens on the selected block.
      selectImageAt(view, wrap);
      const startX = e.clientX;
      const startY = e.clientY;
      const rect = img.getBoundingClientRect();
      const prev = { w: wrap.style.width, h: wrap.style.height };
      let w = Math.round(rect.width);
      let h = Math.round(rect.height);
      // The drag stops at the line's content box — the stored number is the drawn one,
      // in a cell and in the document alike. No layout (0) ⇒ unclamped.
      const cap = wrap.parentElement ? Math.floor(contentBoxWidth(wrap.parentElement)) : 0;
      const capped = (v: number) => (cap > 0 ? Math.min(cap, v) : v);
      const onMove = (ev: PointerEvent) => {
        if (dir !== 'bottom') w = capped(Math.max(24, Math.round(rect.width + ev.clientX - startX)));
        if (dir === 'bottom') h = Math.max(24, Math.round(rect.height + ev.clientY - startY));
        setImageBox(wrap, img, `${w}px`, isCorner(dir) ? undefined : `${h}px`);
      };
      const detach = () => {
        handle.removeEventListener('pointermove', onMove);
        handle.removeEventListener('pointerup', onUp);
        handle.removeEventListener('pointercancel', onCancel);
      };
      const restore = () => { setImageBox(wrap, img, prev.w || undefined, prev.h || undefined); };
      const onUp = () => {
        detach();
        let pos: number;
        try { pos = view.posAtDOM(wrap); } catch { restore(); return; }
        // Images only (tableId === null): guardedAtomicRanges also yields table anchors.
        const atom = pos >= 0 ? atomicRangeAt(view.state, pos) : null;
        const suffix = isCorner(dir) ? String(w) : `${w}x${h}`;
        if (!atom || atom.tableId !== null || !applyImageSuffix(view, atom.from, atom.to, suffix)) restore();
      };
      const onCancel = () => { detach(); restore(); };
      try { handle.setPointerCapture(e.pointerId); } catch { /* detached DOM — listeners below still bound */ }
      handle.addEventListener('pointermove', onMove);
      handle.addEventListener('pointerup', onUp);
      handle.addEventListener('pointercancel', onCancel);
    });
    return handle;
  }

  eq(other: ImageWidget): boolean {
    return this.src === other.src
      && this.width === other.width
      && this.height === other.height;
  }
  ignoreEvent(): boolean { return false; }
  get estimatedHeight(): number { return widgetHeightCache.get(this.src) ?? 200; }
}

// ─── Transclusion widget ──────────────────────────────────────────────────────
// see SYSTEM: transclusion — inline rendering of another document/reference's content.
// ARCH: the body is rendered through the SAME CodeMirror pipeline as the host document
// (a nested, read-only EditorView seeded with the transcluded text + the shared
// render-bundle), so the embed looks exactly as if its content lived in the main doc and
// code blocks wrap within the column (EditorView.lineWrapping). Seeding content as a doc
// string (never innerHTML) makes the body inert by construction — no XSS boundary needed.
// WHY: transclusion renders at most one nesting level. Why: a transcluded doc can
// embed itself or form a cycle; the nested view runs at depth+1 and build-structural
// degrades any embed at depth >= 1 to a plain link (TransclusionLinkWidget).
// Height is cached (unified widgetHeightCache, keyed by entity id) so async content load
// doesn't cause scroll jumps on the next decoration rebuild.

// INVARIANT: the embed renders the source content verbatim except for a single trailing
// newline (the EOF terminator), which is stripped. Why: the two content sources disagree
// on it — the live store doc has none, the REST fetch (fetchDocumentContent) carries one —
// so without this the band grows a phantom empty last line after the host doc is reloaded.
// Stripping exactly one matches the source document's own editor rendering; intentional
// blank lines (\n\n…) are preserved.
function transclusionBodyText(content: string | undefined): string {
  return (content ?? '').replace(/\n$/, '');
}

/** Builds the header row (title + navigation) shared by the band widget. */
function buildTransclusionHeader(
  entityId: string,
  kind: 'doc' | 'ref',
  title: string,
): HTMLElement {
  const titleEl = document.createElement('div');
  titleEl.className = 'cm-transclusion-title';
  titleEl.textContent = title;
  titleEl.title = title;
  // Block CM6 cursor placement on header press so the widget survives until the
  // click fires (otherwise buildStructuralDecorations reveals raw markdown first).
  titleEl.addEventListener('mousedown', (e) => {
    e.preventDefault();
    e.stopPropagation();
  });
  titleEl.addEventListener('click', (e) => {
    e.stopPropagation();
    e.preventDefault();
    if (kind === 'doc') emit('navigate-to-document', { documentId: entityId });
    else emit('navigate-to-reference', { referenceId: entityId });
  });

  const header = document.createElement('div');
  header.className = 'cm-transclusion-header';
  header.appendChild(titleEl);
  return header;
}

/**
 * Replaces a ![alt](ref:id) / ![alt](doc:id) / ![alt](bare-id) node with an
 * embedded content block rendered through a nested read-only EditorView. Header click
 * navigates to the source entity.
 */
export class TransclusionWidget extends WidgetType {
  /** Nested read-only EditorView rendering the body (null while loading). */
  nestedView: EditorView | null = null;
  /** Cancellation token for the pending mountNested rAF; set true in destroy(). */
  private destroyed = false;
  /** Handle of the in-flight rAF (cancelled on destroy to avoid a stale re-measure). */
  private rafHandle: number | null = null;

  constructor(
    readonly entry: TransclusionEntry,
    readonly entityId: string,
    readonly kind: 'doc' | 'ref',
    /** Nesting depth of the HOST view; the nested body runs at depth + 1. */
    readonly depth: number = 0,
  ) { super(); }

  /** Mounts the nested editor into `body` and wires async height stabilization. */
  private mountNested(body: HTMLElement, outerView: EditorView): void {
    this.nestedView = new EditorView({
      parent: body,
      state: EditorState.create({
        doc: transclusionBodyText(this.entry.content),
        extensions: nestedRenderExtensions(this.depth + 1),
      }),
    });
    // The nested view lays out asynchronously (syntax tree builds async); cache the
    // settled height and ask the OUTER view to re-measure so the block-widget height
    // estimate matches reality (prevents scroll jump).
    // ARCH: the rAF closure reads instance fields (`this.destroyed`) via the closure-captured
    // method scope; capture the cancellation flag value into a local so destroy() flipping it
    // is observed. `this.destroyed` is checked at rAF time (after destroy cancels the handle).
    this.rafHandle = requestAnimationFrame(() => {
      if (this.destroyed) return;
      const wrap = body.parentElement;
      if (wrap?.isConnected) {
        widgetHeightCache.set(this.entityId, wrap.offsetHeight);
        if (isViewAlive(outerView)) outerView.requestMeasure();
      }
    });
  }

  toDOM(outerView?: EditorView): HTMLElement {
    const wrap = document.createElement('div');
    const loading = this.entry.content === undefined;
    wrap.className = loading ? 'cm-transclusion cm-transclusion-loading' : 'cm-transclusion';
    wrap.setAttribute('data-transclusion-id', this.entityId);
    wrap.appendChild(buildTransclusionHeader(this.entityId, this.kind, this.entry.title));

    const body = document.createElement('div');
    if (loading) {
      body.className = 'cm-transclusion-body cm-transclusion-spinner';
    } else {
      body.className = 'cm-transclusion-body';
      // outerView is always supplied by CM6 at render time; the `??` guards the unit
      // test that calls toDOM() directly (requestMeasure becomes a no-op there).
      this.mountNested(body, outerView ?? ({ requestMeasure() {} } as EditorView));
      PREV_WIDGETS.set(wrap, this);
    }
    wrap.appendChild(body);
    return wrap;
  }

  /**
   * Reconcile in place: when only the body text changed (same entity/kind), dispatch
   * the new text into the existing nested view instead of tearing it down (preserves
   * scroll + perf). Returns false to force a rebuild on a loading↔loaded transition or
   * an entity change.
   */
  updateDOM(dom: HTMLElement, outerView: EditorView): boolean {
    const wasLoading = dom.classList.contains('cm-transclusion-loading');
    const nowLoading = this.entry.content === undefined;
    if (wasLoading !== nowLoading) return false;
    // Adopt the live nested view from the previous widget instance (CM6 reuses DOM but
    // creates a fresh widget object), then patch text + header.
    const body = dom.querySelector('.cm-transclusion-body') as HTMLElement | null;
    if (!nowLoading && body) {
      const existing = (PREV_WIDGETS.get(dom) ?? null) as TransclusionWidget | null;
      this.nestedView = existing?.nestedView ?? null;
      if (!this.nestedView) {
        body.replaceChildren();
        this.mountNested(body, outerView);
      } else if (this.nestedView.state.doc.toString() !== transclusionBodyText(this.entry.content)) {
        this.nestedView.dispatch({
          changes: { from: 0, to: this.nestedView.state.doc.length, insert: transclusionBodyText(this.entry.content) },
        });
      }
    }
    const titleEl = dom.querySelector('.cm-transclusion-title') as HTMLElement | null;
    if (titleEl && titleEl.textContent !== this.entry.title) {
      titleEl.textContent = this.entry.title;
      titleEl.title = this.entry.title;
    }
    PREV_WIDGETS.set(dom, this);
    return true;
  }

  destroy(dom: HTMLElement): void {
    // ARCH: idempotent — CM6 may call destroy twice (rebuild after updateDOM), and the
    // rAF closure also guards on `destroyed`. Cancel the pending rAF so a stale re-measure
    // never runs after teardown.
    this.destroyed = true;
    if (this.rafHandle !== null) {
      cancelAnimationFrame(this.rafHandle);
      this.rafHandle = null;
    }
    // Idempotent: nestedView is nulled after the first destroy, so a second call is a
    // no-op. The previous widget instance (tracked via PREV_WIDGETS) also sets its
    // nestedView null on destroy, so updateDOM never adopts a destroyed view.
    if (this.nestedView) {
      this.nestedView.destroy();
      this.nestedView = null;
    }
    PREV_WIDGETS.delete(dom);
  }

  eq(other: TransclusionWidget): boolean {
    return this.entityId === other.entityId
      && this.kind === other.kind
      && this.depth === other.depth
      && this.entry.kind === other.entry.kind
      && this.entry.title === other.entry.title
      && this.entry.content === other.entry.content
      && this.entry.imageUrl === other.entry.imageUrl;
  }

  // Let clicks through for header navigation; the header handler stops propagation itself.
  ignoreEvent(): boolean { return false; }

  get estimatedHeight(): number {
    const cached = widgetHeightCache.get(this.entityId);
    if (cached) return cached;
    const lines = this.entry.content ? this.entry.content.split('\n').length : 1;
    return Math.max(120, lines * 22 + 36);
  }
}

// WeakMap dom → last widget instance, so updateDOM can adopt the previous instance's
// live nestedView (CM6 hands updateDOM only the new widget, not the old one).
const PREV_WIDGETS = new WeakMap<HTMLElement, TransclusionWidget>();

/** Liveness check for an EditorView — this CM6 build has no `isDestroyed`, so test the
 * root DOM node's connectivity. A torn-down view's DOM is detached from the document. */
function isViewAlive(view: EditorView | null): view is EditorView {
  return !!view && !!view.dom && view.dom.isConnected;
}

/**
 * Depth-capped fallback: at nesting depth >= 1 an embed renders as a plain inline
 * doc/ref link (no recursion into another band). Click navigates to the source.
 */
export class TransclusionLinkWidget extends WidgetType {
  constructor(
    readonly entry: TransclusionEntry,
    readonly entityId: string,
    readonly kind: 'doc' | 'ref',
  ) { super(); }

  toDOM(): HTMLElement {
    const a = document.createElement('span');
    a.className = this.kind === 'doc' ? 'cm-doc-link' : 'cm-ref-link';
    a.textContent = this.entry.title;
    a.title = this.entry.title;
    a.addEventListener('mousedown', (e) => { e.preventDefault(); e.stopPropagation(); });
    a.addEventListener('click', (e) => {
      e.stopPropagation();
      e.preventDefault();
      if (this.kind === 'doc') emit('navigate-to-document', { documentId: this.entityId });
      else emit('navigate-to-reference', { referenceId: this.entityId });
    });
    return a;
  }

  eq(other: TransclusionLinkWidget): boolean {
    return this.entityId === other.entityId
      && this.kind === other.kind
      && this.entry.title === other.entry.title;
  }
  ignoreEvent(): boolean { return false; }
}

// ─── File-reference widget (see SYSTEM: transclusion — ref-file kind) ────────

// Inline archive icon (the lucide "archive" box), sized to match ref-utils icons.
const ARCHIVE_ICON_SVG = (
  '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24"' +
  ' fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"' +
  ' stroke-linejoin="round"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/>' +
  '<path d="M10 12h4"/></svg>'
);

/**
 * A `![alt](ref:file_ref_id)` embed for a content-less file reference renders as a
 * compact download card (icon + title + size) instead of an empty band — the
 * archive's bytes are its content. The whole card is the download anchor (same
 * plain `<a download>` route as AudioPlayer's download button).
 */
export class FileRefWidget extends WidgetType {
  constructor(
    readonly entry: TransclusionEntry,
    readonly entityId: string,
  ) { super(); }

  toDOM(): HTMLElement {
    const a = document.createElement('a');
    a.className = 'cm-file-ref-card';
    a.style.cssText = (
      'display:inline-flex;align-items:center;gap:6px;padding:3px 10px;' +
      'border:1px solid var(--border-soft, rgba(0,0,0,0.15));' +
      'background:var(--surface2, transparent);color:inherit;' +
      'text-decoration:none;font-size:12px;line-height:1.4;' +
      'vertical-align:middle;margin:2px 0;'
    );
    if (this.entry.fileUrl) {
      a.href = this.entry.fileUrl;
      a.setAttribute('download', this.entry.title);
    }
    const icon = document.createElement('span');
    icon.style.cssText = 'display:inline-flex;flex-shrink:0;color:var(--text-dim, inherit);';
    icon.innerHTML = ARCHIVE_ICON_SVG;
    const title = document.createElement('span');
    title.textContent = this.entry.title;
    title.title = this.entry.title;
    title.style.cssText = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:280px;';
    a.appendChild(icon);
    a.appendChild(title);
    if (this.entry.fileSize !== undefined) {
      const size = document.createElement('span');
      size.textContent = formatFileSize(this.entry.fileSize);
      size.style.cssText = 'color:var(--text-dim, inherit);flex-shrink:0;';
      a.appendChild(size);
    }
    return a;
  }

  eq(other: FileRefWidget): boolean {
    return this.entityId === other.entityId
      && this.entry.kind === other.entry.kind
      && this.entry.title === other.entry.title
      && this.entry.fileUrl === other.entry.fileUrl
      && this.entry.fileSize === other.entry.fileSize;
  }
  ignoreEvent(): boolean { return false; }
}

// ─── Code block copy button widget ────────────────────────────────────────────

/** Copy-to-clipboard button for fenced code blocks. Shows language label if present, otherwise a copy icon. */
export class CopyButtonWidget extends WidgetType {
  constructor(readonly codeText: string, readonly language: string = '') { super(); }

  toDOM(): HTMLElement {
    const btn = document.createElement('button');
    btn.className = this.language ? 'cm-code-copy-btn cm-code-copy-btn--label' : 'cm-code-copy-btn';
    btn.setAttribute('aria-label', 'Copy code');
    if (this.language) {
      btn.textContent = this.language;
    } else {
      btn.innerHTML = COPY_ICON;
    }

    btn.addEventListener('click', (e) => {
      e.preventDefault();
      copyWithToast(
        this.codeText,
        (m, ty) => useAppStore.getState().showToast(m, ty),
        () => {
          if (!this.language) {
            btn.innerHTML = CHECK_ICON;
            setTimeout(() => { btn.innerHTML = COPY_ICON; }, 1500);
          }
        },
      );
    });

    return btn;
  }

  eq(other: CopyButtonWidget): boolean {
    return this.codeText === other.codeText && this.language === other.language;
  }
  ignoreEvent(): boolean { return true; }
}

// ─── Math widgets ─────────────────────────────────────────────────────────────

export class InlineMathWidget extends WidgetType {
  constructor(readonly latex: string) { super(); }

  toDOM(): HTMLElement {
    const span = document.createElement('span');
    span.className = 'cm-math-inline';
    span.innerHTML = renderMathCached(this.latex, false);
    return span;
  }

  eq(other: InlineMathWidget): boolean { return this.latex === other.latex; }
  ignoreEvent(): boolean { return false; }
}

export class BlockMathWidget extends WidgetType {
  constructor(
    readonly latex: string,
    readonly listIndent: string = '0',
  ) { super(); }

  toDOM(): HTMLElement {
    const div = document.createElement('div');
    div.className = 'cm-math-block';
    if (this.listIndent !== '0') {
      div.style.paddingLeft = this.listIndent;
      div.style.paddingRight = this.listIndent;
    }
    div.innerHTML = renderMathCached(this.latex, true);
    return div;
  }

  eq(other: BlockMathWidget): boolean {
    return this.latex === other.latex && this.listIndent === other.listIndent;
  }
  ignoreEvent(): boolean { return false; }
  get estimatedHeight(): number {
    const lines = this.latex.split('\n').length;
    return 40 + lines * 20;
  }
}

// ─── Mermaid widget ──────────────────────────────────────────────────────────

export class MermaidWidget extends WidgetType {
  constructor(readonly code: string) { super(); }

  toDOM(): HTMLElement {
    const div = document.createElement('div');
    div.className = 'cm-mermaid-block';
    renderMermaid(this.code).then((svg) => {
      if (div.isConnected) div.innerHTML = svg;
    });
    return div;
  }

  eq(other: MermaidWidget): boolean { return this.code === other.code; }
  ignoreEvent(): boolean { return false; }
  get estimatedHeight(): number {
    const lines = this.code.split('\n').length;
    return 60 + lines * 24;
  }
}

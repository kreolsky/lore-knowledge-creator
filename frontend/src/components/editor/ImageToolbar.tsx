/**
 * Floating toolbar for ONE selected image embed — the atomic-block companion
 * to SelectionToolbar. Shown while the main selection EQUALS one ImageWidget
 * range (a press on the image or Shift+Arrow selects exactly that; see
 * selectedAtomicRange in atomic-delete-guard.ts). Size field (`300` ⇒ px,
 * `50%` ⇒ percent, empty clears the suffix), Delete (removes the node — NOT
 * the reference), Download (the reference's original file URL). Pinned inside
 * the image's top-left corner.
 *
 * Gated like every writer of the block: full access, not a public share, a
 * view that takes input (same check as getDropGuard / CheckboxWidget) —
 * read-only/preview views get no toolbar. In plain-text mode (Mod-/) the
 * live-preview field is gone, no image range is found, nothing shows.
 */

import type React from 'react';
import { useState, useEffect, useLayoutEffect, useRef, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { EditorView } from '@codemirror/view';
import { Download, Trash2 } from 'lucide-react';
import { useUIStore } from '../../store/ui-store';
import { useAppStore } from '../../store/app-store';
import { useEditorView } from '../../editor/active-editor';
import { isPanelPreviewView } from '../../editor/editor-plugins';
import { on, off } from '../../events';
import { useTranslation } from '../../i18n';
import { referenceFileUrl } from '../../utils/reference-url';
import { FieldInput } from '../ui';
import { selectedAtomicRange } from './live-preview/atomic-delete-guard';
import { applyImageSize, applyImageSuffix, imageEmbedParts, parseSizeFromAlt, SIZE_SUFFIX_RE } from './live-preview/widgets';

const ICON_SIZE = 15;

interface ImageAnchor {
  left: number;
  top: number;
}

interface ImagePanel {
  from: number;
  to: number;
  /** Prefill for the size input: '300', '50%', '500x446' or ''. */
  sizeValue: string;
  /** The width the picture is drawn at — the field's placeholder for an unsized image. */
  renderedWidth: number | null;
  fileUrl: string | null;
  downloadName: string;
  anchor: ImageAnchor;
}

/** The `.cm-image-embed` element rendered for the image node starting at `from`. */
function imageEmbedElement(view: EditorView, from: number): HTMLElement | null {
  for (const el of view.contentDOM.querySelectorAll<HTMLElement>('.cm-image-embed')) {
    try { if (view.posAtDOM(el) === from) return el; } catch { /* detached */ }
  }
  return null;
}

/** Reads the selection → panel payload, or null when the selection is not exactly one image. */
function readPanel(view: EditorView): ImagePanel | null {
  // Images only (tableId === null): selectedAtomicRange also matches a whole-selected table.
  const range = selectedAtomicRange(view.state);
  if (!range || range.tableId !== null) return null;
  const parts = imageEmbedParts(view.state.doc.sliceString(range.from, range.to));
  if (!parts) return null;
  const { width, height } = parseSizeFromAlt(parts.alt);
  const bare = (v: string) => (v.endsWith('px') ? v.slice(0, -2) : v);
  // WHY both axes: a non-proportional size must round-trip through the field —
  // showing the width alone and writing it back would silently keep the height.
  const sizeValue = width ? bare(width) + (height ? `x${bare(height)}` : '') : '';
  // Image embeds are `ref:<id>` (build-structural routes only resolved image
  // refs here); keep the slice defensive for any bare-id form.
  const refId = parts.url.startsWith('ref:') ? parts.url.slice(4) : parts.url;
  const ref = useAppStore.getState().references.find((r) => r.reference_id === refId);
  const fileUrl = ref?.file_path ? referenceFileUrl(ref.reference_id, ref.file_path) : null;
  const downloadName = ref?.file_meta?.original_name || ref?.title || 'image';

  // WHY the widget's DOM, not coordsAtPos: text coords describe the line box,
  // not the picture; the embed wrapper's rect IS the image.
  const embed = imageEmbedElement(view, range.from);
  const rect = embed?.getBoundingClientRect() ?? view.coordsAtPos(range.from);
  if (!rect) return null;
  return {
    from: range.from,
    to: range.to,
    sizeValue,
    renderedWidth: embed ? Math.round(rect.right - rect.left) : null,
    fileUrl,
    downloadName,
    anchor: { left: rect.left, top: rect.top },
  };
}

export default function ImageToolbar() {
  const { t } = useTranslation();
  const getEditorView = useEditorView();
  const selectionEmpty = useUIStore((s) => s.selectionEmpty);
  const isPublicShare = useUIStore((s) => s.isPublicShare);
  const accessLevel = useAppStore((s) => s.accessLevel);
  const canEdit = accessLevel === 'full' && !isPublicShare;

  const [panel, setPanel] = useState<ImagePanel | null>(null);
  const [sizeDraft, setSizeDraft] = useState('');
  const [sizeError, setSizeError] = useState(false);
  const barRef = useRef<HTMLDivElement>(null);

  const hide = useCallback(() => {
    setPanel(null);
    setSizeError(false);
  }, []);

  const refresh = useCallback(() => {
    const view = getEditorView();
    if (!view) { setPanel(null); return; }
    const next = readPanel(view);
    setPanel((prev) => {
      // A DIFFERENT image selected → adopt its size as the new draft. The same
      // image with a CHANGED size (handle resize: same-length suffix, same
      // range) adopts it too — unless the size field owns focus: the user is
      // mid-typing and a remote/re-derived size must not clobber the draft.
      const otherImage = !prev || prev.from !== next?.from || prev.to !== next?.to;
      const sizeMoved = !!prev && prev.sizeValue !== next?.sizeValue
        && !barRef.current?.contains(document.activeElement);
      if (next && (otherImage || sizeMoved)) {
        setSizeDraft(next.sizeValue);
        setSizeError(false);
      }
      return next;
    });
  }, [getEditorView]);

  // Show/update on selection changes; the doc-changed catch-up re-anchors the
  // panel when a remote edit shifts the selected image.
  useEffect(() => {
    on('editor-selection-change', refresh);
    on('editor-doc-changed', refresh);
    return () => {
      off('editor-selection-change', refresh);
      off('editor-doc-changed', refresh);
    };
  }, [refresh]);

  useEffect(() => {
    if (selectionEmpty) {
      // HOLD while the size field owns focus: blurring into the field flips the
      // selection feed's live gate and would close the panel mid-typing. Any
      // collapse that happens with focus elsewhere is a real dismiss.
      if (!barRef.current?.contains(document.activeElement)) setPanel(null);
      return;
    }
    refresh();
  }, [selectionEmpty, refresh]);

  // Any scroll (document, split column, page) moves the image: re-anchor to it.
  const shown = panel !== null;
  useEffect(() => {
    if (!shown) return;
    document.addEventListener('scroll', refresh, true);
    return () => document.removeEventListener('scroll', refresh, true);
  }, [shown, refresh]);

  // Escape dismisses.
  useEffect(() => {
    if (!panel) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') hide(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [panel, hide]);

  // Click outside dismisses — except inside any editor: there the selection
  // feed decides (clicking ANOTHER image re-selects and re-shows; clicking text
  // collapses the selection and hides via the selectionEmpty path).
  useEffect(() => {
    if (!panel) return;
    const onDown = (e: MouseEvent) => {
      if (barRef.current?.contains(e.target as Node)) return;
      if ((e.target as Element | null)?.closest?.('.cm-editor')) return;
      hide();
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [panel, hide]);

  // Final position post-measure: inside the image's top-left corner, clamped
  // to the viewport.
  useLayoutEffect(() => {
    const el = barRef.current;
    if (!panel || !el) return;
    const x = Math.max(8, Math.min(panel.anchor.left + 6, window.innerWidth - el.offsetWidth - 8));
    const y = Math.max(8, Math.min(panel.anchor.top + 6, window.innerHeight - el.offsetHeight - 8));
    el.style.left = `${x}px`;
    el.style.top = `${y}px`;
  }, [panel, sizeError]);

  const view = getEditorView();
  if (!panel || !view || !canEdit) return null;
  // Same editable gate as getDropGuard / CheckboxWidget: no toolbar on a view
  // that cannot take input (collab joining, disconnected, read-only preview).
  if (view.state.readOnly || !view.state.facet(EditorView.editable) || isPanelPreviewView(view)) {
    return null;
  }

  const applySize = () => {
    if (!panel) return;
    const v = sizeDraft.trim();
    if (v !== '' && !SIZE_SUFFIX_RE.test(v)) {
      setSizeError(true);
      return;
    }
    view.focus();
    // A bare width is proportional (drops an authored height, like the corner
    // handle); `WxH` sets both axes.
    if (v === '') applyImageSize(view, panel.from, panel.to, '');
    else applyImageSuffix(view, panel.from, panel.to, v);
    // The dispatch re-selected the new node range; adopt it (the event feed
    // stays quiet while the field held focus — the selection feed's live gate).
    const next = readPanel(view);
    setPanel(next);
    if (next) setSizeDraft(next.sizeValue);
    setSizeError(false);
  };

  const deleteEmbed = () => {
    if (!panel) return;
    // Removes the EMBED NODE only — the reference and its file stay in the
    // project (their actions live on the reference surfaces).
    view.dispatch({ changes: { from: panel.from, to: panel.to } });
    view.focus();
    hide();
  };

  return createPortal(
    <div
      ref={barRef}
      data-testid="image-toolbar"
      className="selection-toolbar left-0 top-0"
    >
      <FieldInput
        className="w-24"
        compact
        name="image-size"
        value={sizeDraft}
        // WHY a placeholder, not a value: a value is written back on blur and would
        // add a suffix nobody chose. The format hint stays reachable as the title.
        placeholder={panel.renderedWidth ? String(panel.renderedWidth) : t('imageSizePlaceholder')}
        title={t('imageSizePlaceholder')}
        inputMode="decimal"
        aria-label={t('imageSize')}
        onChange={(e) => { setSizeDraft(e.target.value); setSizeError(false); }}
        onKeyDown={(e) => {
          if (e.key === 'Enter') { e.preventDefault(); applySize(); }
        }}
      />
      <button
        className="sel-toolbar-btn"
        title={t('removeImageEmbed')}
        aria-label={t('removeImageEmbed')}
        onClick={deleteEmbed}
      >
        <Trash2 size={ICON_SIZE} />
      </button>
      {panel.fileUrl && (
        <a
          className="sel-toolbar-btn"
          href={panel.fileUrl}
          download={panel.downloadName}
          title={t('download')}
          aria-label={t('download')}
          onClick={hide}
        >
          <Download size={ICON_SIZE} />
        </a>
      )}
      {sizeError && (
        <span className="px-2 text-ui-sm text-red" role="alert">{t('imageSizeInvalid')}</span>
      )}
    </div>,
    document.body,
  );
}

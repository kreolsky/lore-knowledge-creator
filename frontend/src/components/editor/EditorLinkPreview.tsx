/**
 * Hover preview for links (documents, references, notes, external URLs).
 *
 * Serves BOTH the CM6 editor decorations and chat links (in-answer markdown links tagged
 * by MarkdownContent + sources list items tagged by Sources). Listens via event
 * delegation on the document for mouseenter/mouseleave (capture) on elements carrying the
 * matching class + data-link-type attribute. Shows a plain-text/image preview popup
 * positioned above or below the link after a 300ms delay.
 *
 * // ARCH: Link metadata (type + id/url) stored in state; content derived in useMemo
 * // from stores (ref, note) or useDocumentPreview hook (doc). This unifies sync/async
 * // resolution — the showPreview callback only sets positions/meta, never content.
 * // ARCH: Position rule — above cursor aligns bottom edge; below cursor aligns top edge.
 * // ARCH: Horizontal placement is dual (chooseLinkPlacement): links INSIDE
 * // <aside class="right-panel"> (chat sources / chat markdown links) pin to the
 * // SIDE of the panel — same formula + shift as useRightPanelHoverPreview
 * // (Notes/Refs card previews); editor links stay near the link.
 * // ARCH: data-link-id contract — centralized in utils/in-app-link.ts. Docs use a bare id;
 * // refs/notes keep their scheme prefix. decodeLinkId strips any prefix defensively for all
 * // types, so both bare and prefixed values resolve correctly regardless of the producer.
 * // ARCH: two metadata branches — attributed elements (.chat-link/.chat-source/cm-*) read
 * // data-link-type/id; dsh chat anchors read the rewritten lore.local href instead (they
 * // carry no attributes we control) and derive the same LinkMeta via parseInAppLink.
 */
// SYSTEM: editor-link-preview — global hover-preview host for editor + chat links

import { useState, useRef, useCallback, useEffect, useMemo } from 'react';
import { LinkPreviewPopup } from '../LinkPreviewPopup';
import { useAppStore } from '../../store/app-store';
import { useNoteChatStore } from '../../store/note-chat-store';
import { useDocumentPreview } from '../../hooks/useDocumentPreview';
import { useDocumentTitle } from '../../hooks/useDocumentTitle';
import { useReferencePreview } from '../../hooks/useReferencePreview';
import { usePopupSlot } from '../../hooks/usePopupSlot';
import { computePopupPosition, chooseLinkPlacement } from '../../utils/popup-position';
import { PREVIEW_MAX_HEIGHT } from '../../utils/preview-geometry';
import { decodeLinkId, parseInAppLink } from '../../utils/in-app-link';
import { referenceFileUrl } from '../../utils/reference-url';
import { isTouchPointer } from '../../utils/last-pointer';

// Editor link decorations (CM6) + chat link/source elements tagged with data-link-type
// + dsh chat anchors: internal doc:/ref:/note: links the chat markdown wrapper rewrote
// to https://lore.local/l/<type>/<id> (matched by href — dsh owns those anchors' classes).
const EVERY_LINK_SELECTOR =
  '.cm-doc-link,.cm-ref-link,.cm-note-link,.cm-ext-link,.cm-doc-link-broken,.cm-ref-link-broken,.cm-note-link-broken,.chat-link,.chat-source,.chat-markdown a[href^="https://lore.local/l/"]';
const DELAY_MS = 300;

interface LinkMeta {
  type: 'doc' | 'ref' | 'note' | 'ext';
  id: string;
}

interface PreviewState {
  top?: number;
  bottom?: number;
  left: number;
  maxHeight: number;
  meta: LinkMeta | null;
}

const NO_PREVIEW: PreviewState = { top: 0, left: 0, maxHeight: 0, meta: null };

export function EditorLinkPreview() {
  // WHY: read the active document id from the store, not useParams(). This component is
  // rendered once at ProjectPage level (above the docs/:documentId route), so useParams()
  // would not see documentId and the hide-on-navigation effect below would never fire.
  const documentId = useAppStore(s => s.currentDocument?.document_id);
  const currentRefId = useAppStore(s => s.currentReference?.reference_id);
  const references = useAppStore(s => s.references);
  const noteChatSessions = useNoteChatStore(s => s.sessions);

  const [visible, setVisible] = useState(false);
  const [state, setState] = useState<PreviewState>(NO_PREVIEW);

  const meta = state.meta;
  const previewDocId = meta?.type === 'doc' ? decodeLinkId('doc', meta.id) : null;
  const { content: docContent, error: docError, loading: docLoading } = useDocumentPreview(previewDocId);
  const docTitle = useDocumentTitle(previewDocId);

  // Cross-document references aren't in the ancestor-scoped store; lazy-fetch on a store
  // miss so their hover preview resolves (mirrors useDocumentPreview for doc links). The
  // store list is metadata-only, so in-store text refs also fetch their body through the
  // preview cache — UNLESS the store already has a usable body (image w/ file_path, or text
  // already hydrated). A ref NOT in the store (cross-doc) always fetches (unknown body).
  const previewRefId = meta?.type === 'ref' ? decodeLinkId('ref', meta.id) : null;
  const inStoreRef = previewRefId ? references.find(r => r.reference_id === previewRefId) ?? null : null;
  const hasStoreBody = !!inStoreRef
    && ((inStoreRef.media_type === 'image' && inStoreRef.file_path) || inStoreRef.content !== undefined);
  const fetchId = !hasStoreBody ? previewRefId : null;
  const { preview: fetchedRefPreview, error: refError, loading: refLoading } = useReferencePreview(fetchId);

  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const currentElRef = useRef<HTMLElement | null>(null);
  const trackedElRef = useRef<HTMLElement | null>(null);
  const visibleRef = useRef(false);
  const popupRef = useRef<HTMLDivElement | null>(null);

  visibleRef.current = visible;

  // INVARIANT(no-silent-degradation): error/loading are carried per link TYPE, never
  // inferred from an undefined `content`. Why: a note/ref/ext link leaves docContent
  // undefined forever (previewDocId is null — the body comes from the store), so an
  // inferred "loading" would spin that popup permanently. Only the branch that actually
  // owns a fetch reports its state.
  const resolved: { title?: string; content?: string; imageUrl?: string; error?: boolean; loading?: boolean } = useMemo(() => {
    if (!meta) return {};
    if (meta.type === 'doc') return { title: docTitle, content: docContent, error: docError, loading: docLoading };
    if (meta.type === 'ref') {
      const refId = decodeLinkId('ref', meta.id);
      const ref = references.find(r => r.reference_id === refId);
      if (ref?.media_type === 'image' && ref.file_path)
        return { title: ref.title, imageUrl: referenceFileUrl(refId, ref.file_path) };
      // Hydrated in store → instant; otherwise the lazy-fetched preview (cache).
      if (ref?.content !== undefined) return { title: ref.title, content: ref.content };
      return { ...(fetchedRefPreview ?? {}), title: ref?.title ?? fetchedRefPreview?.title, error: refError, loading: refLoading };
    }
    if (meta.type === 'note') {
      const noteId = decodeLinkId('note', meta.id);
      const session = noteChatSessions.find(s => s.session_id === noteId);
      // WHY: notes carry no title; the panel hover (NotesPanel) titles the popup with the
      // first message and fills it with last_message_preview. Unify the editor hover with
      // that format so it never shows the generic "No content" fallback for a note that
      // has messages.
      const preview = session?.last_message_preview?.trim()
        || session?.first_message_preview?.trim();
      return preview ? { title: session?.first_message_preview?.trim(), content: preview } : {};
    }
    if (meta.type === 'ext') return { content: meta.id };
    return {};
  }, [meta, docTitle, docContent, docError, docLoading, references, noteChatSessions, fetchedRefPreview, refError, refLoading]);

  const clearTimer = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const hide = useCallback(() => {
    clearTimer();
    setVisible(false);
    setState(NO_PREVIEW);
    currentElRef.current = null;
    trackedElRef.current = null;
    popupRef.current = null;
  }, [clearTimer]);

  const showPreview = useCallback((el: HTMLElement) => {
    const linkType = el.getAttribute('data-link-type') as LinkMeta['type'] | null;
    const linkId = el.getAttribute('data-link-id');
    const linkUrl = el.getAttribute('data-link-url');

    let metaObj: LinkMeta;
    if (linkType && linkId) {
      if (linkType === 'doc') metaObj = { type: 'doc', id: linkId };
      else if (linkType === 'ref') metaObj = { type: 'ref', id: linkId };
      else if (linkType === 'note') metaObj = { type: 'note', id: linkId };
      else metaObj = { type: 'ext', id: linkUrl || linkId };
    } else {
      // dsh chat anchors carry no data-link-* attributes — their internal
      // scheme was rewritten to the lore.local URL form (plan
      // chat-markdown-on-dsh); derive the meta from the href. Raw ids decode
      // cleanly through decodeLinkId's defensive prefix strip.
      const href = el instanceof HTMLAnchorElement ? el.getAttribute('href') : null;
      const parsed = href !== null ? parseInAppLink(href) : null;
      if (!parsed) return;
      metaObj = parsed;
    }

    trackedElRef.current = el;
    const rect = el.getBoundingClientRect();
    const pos = computePopupPosition(rect, PREVIEW_MAX_HEIGHT);
    // ARCH: dual placement via
    // chooseLinkPlacement — links rendered INSIDE <aside class="right-panel">
    // (chat sources .chat-source + chat markdown links .chat-link) open pinned to
    // the SIDE of the panel (identical x-offset to Notes/Refs card previews),
    // with the shared RIGHT_PANEL_PREVIEW_SHIFT vertical nudge. Editor links
    // (cm-editor) keep near-link placement. One formula, shared with
    // useRightPanelHoverPreview, so the two popups cannot drift apart.
    const { left, topShift } = chooseLinkPlacement(el, rect);
    setState({
      top: pos.top != null ? pos.top - topShift : undefined,
      bottom: pos.bottom != null ? pos.bottom - topShift : undefined,
      left,
      maxHeight: pos.maxH,
      meta: metaObj,
    });
    setVisible(true);
  }, []);

  const popupRootCallback = useCallback((el: HTMLDivElement | null) => {
    popupRef.current = el;
  }, []);

  const handleMouseEnter = useCallback((e: Event) => {
    // Document-level listener: e.target may be a text node / document (no .closest).
    if (!(e.target instanceof Element)) return;
    // WHY: a touch tap's compat mouseenter is not a hover — the tap follows the link.
    if (isTouchPointer()) return;
    const el = e.target.closest(EVERY_LINK_SELECTOR) as HTMLElement | null;
    if (!el) return;

    currentElRef.current = el;
    clearTimer();

    timerRef.current = setTimeout(() => {
      if (currentElRef.current === el) {
        showPreview(el);
      }
    }, DELAY_MS);
  }, [clearTimer, showPreview]);

  const handleMouseLeave = useCallback((e: Event) => {
    if (!(e.target instanceof Element)) return;
    const el = e.target.closest(EVERY_LINK_SELECTOR) as HTMLElement | null;
    if (!el) return;
    if (currentElRef.current !== el) return;

    const me = e as MouseEvent;
    if (visibleRef.current && popupRef.current) {
      if (popupRef.current.contains(me.relatedTarget as Node | null)) return;
    }

    hide();
  }, [hide]);

  // Hide preview when navigating to another document or reference
  useEffect(() => {
    hide();
  }, [documentId, currentRefId, hide]);

  // Hide preview when the editor link element is removed from the DOM (e.g. link formatting
  // deleted). Editor-only: chat links live in .chat-markdown / .chat-sources containers that
  // rebuild via dangerouslySetInnerHTML on every streaming token — node identity is unstable
  // there, so observing them would dismiss the preview on every token. Chat previews rely on
  // mouseleave + outside-mousedown dismissal instead.
  useEffect(() => {
    if (!visible || !trackedElRef.current) return;

    const el = trackedElRef.current;
    const editorRoot = el.closest('.cm-editor');
    if (!editorRoot) return;
    const observer = new MutationObserver(() => {
      if (trackedElRef.current && !editorRoot.contains(trackedElRef.current)) {
        hide();
      }
    });
    observer.observe(editorRoot, { childList: true, subtree: true });

    return () => observer.disconnect();
  }, [visible, hide]);

  // Shares the lowest-priority 'hover-preview' slot — gating visibility yields to any
  // picker/suggestions popup without clearing state during the one-render acquire lag.
  const isActive = usePopupSlot('hover-preview', visible);

  useEffect(() => {
    document.addEventListener('mouseenter', handleMouseEnter as EventListener, { capture: true });
    document.addEventListener('mouseleave', handleMouseLeave as EventListener, { capture: true });

    return () => {
      hide();
      document.removeEventListener('mouseenter', handleMouseEnter as EventListener, { capture: true });
      document.removeEventListener('mouseleave', handleMouseLeave as EventListener, { capture: true });
    };
  }, [handleMouseEnter, handleMouseLeave, hide]);

  return (
    <LinkPreviewPopup
      visible={visible && isActive}
      top={state.top}
      bottom={state.bottom}
      left={state.left}
      maxHeight={state.maxHeight}
      title={resolved.title}
      content={resolved.content}
      imageUrl={resolved.imageUrl}
      error={resolved.error}
      loading={resolved.loading}
      popupRef={popupRootCallback}
      onDismiss={hide}
    />
  );
}

/** Scroll to the heading named by the URL hash (#slug) after a document opens.
 *
 * Consumes `useLocation().hash` one-shot per (documentId, hash) pair: the same
 * hash never re-fires on re-renders or SPA navigation, but pasting a new URL
 * (new hash) does. Slugs are resolved on demand from the live editor content
 * (see SYSTEM: heading-slug); an unresolvable slug (heading renamed/deleted)
 * shows a warning toast — never silent (no-silent-degradation rule).
 */

import { useEffect, useRef } from 'react';
import { useLocation } from 'react-router-dom';
import type { EditorView } from '@codemirror/view';
import { useAppStore } from '../store/app-store';
import { emit } from '../events';
import { t } from '../i18n';
import { extractHeadings } from '../editor/extract-headings';
import { resolveSlugToLine } from '../utils/heading-slug';
import { useDocumentRoute } from './useDocumentRoute';

interface UseAnchorScrollParams {
  editorViewRef: React.RefObject<EditorView | null>;
  currentDocument: { document_id: string } | null;
  /** false on the split-view secondary column — anchors target the document only. */
  enabled?: boolean;
}

// WHY: bounded retry for the editor-mount race — the hash is known before the
// CM6 view exists; same precedent as the scrollToOffset delay in useEditorEvents.
const RETRY_MS = 100;
const RETRY_CAP = 10;

export function useAnchorScroll({ editorViewRef, currentDocument, enabled = true }: UseAnchorScrollParams) {
  const { hash } = useLocation();
  const { documentId: routeDocId } = useDocumentRoute();
  const consumedRef = useRef<string | null>(null);

  const docId = currentDocument?.document_id;

  useEffect(() => {
    if (!enabled || !hash || !docId || docId !== routeDocId) return;
    const key = `${docId}|${hash}`;
    if (consumedRef.current === key) return;
    consumedRef.current = key;

    const slug = decodeURIComponent(hash.slice(1));
    if (!slug) return;

    let attempts = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tryScroll = () => {
      const view = editorViewRef.current;
      if (!view) {
        attempts += 1;
        if (attempts >= RETRY_CAP) {
          useAppStore.getState().showToast(t('headingNotFound'), 'warning');
          return;
        }
        timer = setTimeout(tryScroll, RETRY_MS);
        return;
      }
      const line = resolveSlugToLine(extractHeadings(view.state.doc.toString()), slug);
      if (line === null) {
        useAppStore.getState().showToast(t('headingNotFound'), 'warning');
        return;
      }
      emit('scroll-to-line', { line });
    };

    tryScroll();
    return () => { if (timer) clearTimeout(timer); };
  }, [enabled, hash, docId, routeDocId, editorViewRef]);
}

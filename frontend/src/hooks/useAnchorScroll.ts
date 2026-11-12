/** Resolve the URL hash after a document opens: `#<slug>` scrolls to the heading,
 * `#text=…` selects the quoted passage (see utils/link-fragment.ts).
 *
 * Consumes the hash one-shot per location entry (`location.key`): re-renders never
 * re-fire it, a new navigation — even to the same URL — does. Slugs are resolved on
 * demand from the live editor content (see SYSTEM: heading-slug); an anchor that does
 * not resolve (heading renamed, passage edited away) shows a warning toast — never
 * silent (no-silent-degradation rule).
 */

import { useEffect, useRef } from 'react';
import { useLocation } from 'react-router-dom';
import { EditorView } from '@codemirror/view';
import { useAppStore } from '../store/app-store';
import { emit } from '../events';
import { t } from '../i18n';
import { extractHeadings } from '../editor/extract-headings';
import { resolveSlugToLine } from '../utils/heading-slug';
import { isTextFragment, parseTextFragment, resolveTextFragment } from '../utils/link-fragment';
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
  const { hash, key: locationKey } = useLocation();
  const { documentId: routeDocId } = useDocumentRoute();
  const consumedRef = useRef<string | null>(null);

  const docId = currentDocument?.document_id;

  useEffect(() => {
    if (!enabled || !hash || !docId || docId !== routeDocId) return;
    // WHY location.key, not the hash: clicking the same pointer link twice pushes the
    // same URL with a new key, and the second click must select the passage again.
    if (consumedRef.current === locationKey) return;
    consumedRef.current = locationKey;

    const fragment = hash.slice(1);
    if (!fragment) return;
    const target = isTextFragment(fragment) ? textTarget(fragment) : slugTarget(fragment);
    if (!target) return;

    let attempts = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;

    // WHY a miss also retries: the view can mount before collab has synced its text.
    const tryScroll = () => {
      const view = editorViewRef.current;
      if (!view || !target.apply(view)) {
        attempts += 1;
        if (attempts >= RETRY_CAP) {
          useAppStore.getState().showToast(t(target.missKey), 'warning');
          return;
        }
        timer = setTimeout(tryScroll, RETRY_MS);
      }
    };

    tryScroll();
    return () => { if (timer) clearTimeout(timer); };
  }, [enabled, hash, locationKey, docId, routeDocId, editorViewRef]);
}

interface AnchorTarget {
  /** Scroll/select in `view`; false when the anchor does not resolve (yet). */
  apply(view: EditorView): boolean;
  missKey: 'headingNotFound' | 'fragmentNotFound';
}

function slugTarget(fragment: string): AnchorTarget | null {
  let slug: string;
  try { slug = decodeURIComponent(fragment); } catch { slug = fragment; }
  if (!slug) return null;
  return {
    missKey: 'headingNotFound',
    apply(view) {
      const line = resolveSlugToLine(extractHeadings(view.state.doc.toString()), slug);
      if (line === null) return false;
      emit('scroll-to-line', { line });
      return true;
    },
  };
}

function textTarget(fragment: string): AnchorTarget {
  const parsed = parseTextFragment(fragment);
  return {
    missKey: 'fragmentNotFound',
    apply(view) {
      const range = parsed && resolveTextFragment(view.state.doc.toString(), parsed);
      if (!range) return false;
      view.dispatch({
        selection: { anchor: range.from, head: range.to },
        effects: EditorView.scrollIntoView(range.from, { y: 'center' }),
      });
      view.focus();
      if (!view.hasFocus) selectOnPage(view, range.from, range.to);
      return true;
    },
  };
}

// WHY: selection is the browser's native ::selection (no drawSelection, see
// thick-cursor.ts) and CM writes the DOM selection only into a FOCUSED view — a
// non-editable (commentator) view never takes focus, so the dispatched range stays
// invisible. Put it on the page selection by hand once its lines are drawn: the same
// selection a commentator makes with the mouse, which the note hotkeys already read.
function selectOnPage(view: EditorView, from: number, to: number, attempt = 0): void {
  requestAnimationFrame(() => {
    if (!view.dom.isConnected || attempt >= RETRY_CAP) return;
    if (from < view.viewport.from || to > view.viewport.to) {
      selectOnPage(view, from, to, attempt + 1);
      return;
    }
    const start = view.domAtPos(from);
    const end = view.domAtPos(to);
    document.getSelection()?.setBaseAndExtent(start.node, start.offset, end.node, end.offset);
  });
}

/**
 * Reactive list of the current document's tables, derived from the LIVE ydoc for the
 * References-panel "table badges".
 *
 * see SYSTEM: table-block — the panel-facing reactive half of `listDocumentTables`. The pure
 * derivation lives there; this hook wires it to React so a badge list stays in sync with
 * collaborative edits (rows added, anchors deleted) without a second REST round-trip.
 *
 * ARCH: a table is a CRDT subtree of the document, NOT a separate entity, so its list is
 * read from the document's ENTITY handle (`getEntityHandle(documentId)?.ydoc`) — identity,
 * not focus: with a reference column focused the focused slot holds the ref's handle and
 * must not leak into the document badge list. This hook observes both roots that affect
 * the list (`tables` model + `content` anchors) and re-derives on change.
 *
 * PUBLIC BRANCH (/docs/<id>): there is no collab ydoc and none will ever arrive —
 * PublicEditor seeds a preview-local Y.Doc it never publishes. The panel's badges derive
 * STATICALLY from the hydrated `currentDocument` (`content` anchors + `tables_json`
 * captured server-side), mirroring PublicEditor::seedPublicTablesDoc but seeding the
 * `content` text too (listDocumentTables reads BOTH the tables map and content anchors —
 * without the text, unlinked-vs-linked grouping and anchor order are lost). The throwaway
 * Y.Doc is created, derived and destroyed inside one memo — static data per fetch, so no
 * observers, no polling, no registry entry (faking the fat EntityYjsState collab interface
 * for static data would be worse than this second path). Doc-scoped via currentDocument,
 * so badges survive an open reference (authed parity).
 *
 * INVARIANT: the entity handle is published asynchronously (after collab init), so on
 * first render it may be null. Why: the badge list must stay empty-but-alive (distinct
 * from an error state) until the ydoc lands. The hook subscribes via
 * `subscribeEntityHandle(documentId)` — fires immediately with the current value, then
 * on every publish/replace — and attaches observers once the ydoc lands.
 */

import { useEffect, useMemo, useReducer } from 'react';
import * as Y from 'yjs';
import { getEntityHandle, subscribeEntityHandle } from '../collab/active-handle-registry';
import { applyTablesJson, getTablesMap, listDocumentTables } from '../components/editor/live-preview/table-block-model';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';

export function useDocumentTables(documentId: string | null | undefined): ReturnType<typeof listDocumentTables> {
  const [, bump] = useReducer((n: number) => n + 1, 0);
  const isPublicShare = useUIStore(s => s.isPublicShare);
  // PUBLIC seed source. On authed the selector is a constant null — currentDocument
  // churn never re-renders this hook's callers through it.
  const publicDoc = useAppStore(s => (isPublicShare ? s.currentDocument : null));

  // PUBLIC: static derive per document payload (identity-keyed memo — a re-fetch
  // produces a new currentDocument object, a transient re-render does not).
  // Same useMemo-creates-a-Y.Doc precedent as PublicEditor's extensions memo.
  const publicTables = useMemo<ReturnType<typeof listDocumentTables>>(() => {
    if (!isPublicShare || !documentId || !publicDoc || publicDoc.document_id !== documentId) return [];
    const ydoc = new Y.Doc();
    try {
      if (publicDoc.tables_json) applyTablesJson(ydoc, publicDoc.tables_json);
      const content = publicDoc.content ?? '';
      if (content) ydoc.getText('content').insert(0, content);
      return listDocumentTables(ydoc);
    } finally {
      ydoc.destroy();
    }
    // publicDoc object identity covers document_id + content + tables_json.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPublicShare, documentId, publicDoc]);

  useEffect(() => {
    if (!documentId || isPublicShare) return;

    let cancelled = false;
    let unsubscribe: (() => void) | null = null;
    let attachedDoc: Y.Doc | null = null;

    const attach = (doc: Y.Doc): void => {
      if (unsubscribe) unsubscribe();
      const tables = getTablesMap(doc);
      const content = doc.getText('content');
      const cb = (): void => { if (!cancelled) bump(); };
      tables.observeDeep(cb);
      content.observe(cb);
      unsubscribe = () => {
        tables.unobserveDeep(cb);
        content.unobserve(cb);
      };
      attachedDoc = doc;
      cb(); // initial derive
    };

    // Subscribe to THIS document's entity handle (identity, not focus): with a
    // reference focused the slot holds the ref's handle — the badge list still lists
    // the DOCUMENT's tables. Fires immediately with the current value, so a handle
    // that landed before mount attaches in the same tick; a REPLACED handle
    // (re-join) re-attaches onto the new ydoc.
    const unsubEntity = subscribeEntityHandle(documentId, (handle) => {
      if (cancelled) return;
      if (handle?.ydoc && handle.ydoc !== attachedDoc) attach(handle.ydoc);
    });

    return () => {
      cancelled = true;
      unsubEntity();
      unsubscribe?.();
      unsubscribe = null;
      attachedDoc = null;
    };
  }, [documentId, isPublicShare]);

  if (isPublicShare) return publicTables;
  if (!documentId) return [];
  const handle = getEntityHandle(documentId);
  return handle?.ydoc ? listDocumentTables(handle.ydoc) : [];
}

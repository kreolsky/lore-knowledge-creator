/**
 * usePublicTransclusionSync — seeds the module-level `transcludeMap` on /s/:token
 * from already-loaded public data, with lazy fetches for embedded sibling-doc bodies.
 *
 * SYSTEM: transclusion (public-viewer writer) — the /s/:token Editor cannot
 * mount `useEditorReferenceSync` (its project-ref fetch + /documents/batch are
 * authed → 401 anonymously, per the existing PublicEditor comment). This hook
 * is the public-path counterpart: it owns the seed of `transcludeMap` from the
 * store's `documents` + `references` (already loaded by PublicSharePage) and
 * the lazy fetch of embedded sibling-doc bodies via `publicDocumentByDoc`.
 *
 * ARCH: mirrors the authed `useEditorReferenceSync` shape but with public
 * endpoints and no batch. The pure seed logic lives in `buildPublicTransclusionMap`
 * (unit-testable in isolation); this hook owns the effect wiring:
 *   1. compute the next-state map (pure call)
 *   2. diff against the module-level `transcludeMap` (skip dispatch if unchanged)
 *   3. clear-on-unmount so a later authed mount starts from a clean map
 *   4. lazy `publicDocumentByDoc(id)` for `doc:`/bare-id embeds still loading
 *
 * INVARIANT: on 404/failure leave the entry loading — no toast. Why: an out-of-
 * scope `doc:` target (a sibling moved out of the subtree between the tree
 * fetch and the embed render) 404s and MUST NOT error the page. Mirrors the
 * authed "leave loading on failure" catch posture.
 */

import { useEffect } from 'react';
import type { EditorView } from '@codemirror/view';
import type { RefObject } from 'react';
import { useAppStore } from '../store/app-store';
import { publicDocumentByDoc } from '../api/public-share';
import {
  transcludeMap,
  linkContextChanged,
  validDocIds,
  validRefIds,
  type TransclusionEntry,
} from '../components/editor/live-preview';
import { buildPublicTransclusionMap } from '../components/editor/live-preview/build-public-transclusion';
import { parseTarget } from '../components/editor/live-preview/transclusion-grammar';
import { setsEqual } from '../components/editor/live-preview/set-equality';
import { getPublicFileContext } from '../utils/reference-url';

// Value equality for the RENDERED fields (mirrors useEditorReferenceSync).
type EntryRender = Pick<TransclusionEntry, 'kind' | 'title' | 'content' | 'imageUrl'>;
function entriesEqual(a: EntryRender, b: EntryRender): boolean {
  return a.kind === b.kind
    && a.title === b.title
    && a.content === b.content
    && a.imageUrl === b.imageUrl;
}
function mapsEqual(
  a: ReadonlyMap<string, TransclusionEntry>,
  b: ReadonlyMap<string, TransclusionEntry>,
): boolean {
  if (a.size !== b.size) return false;
  for (const [k, v] of a) {
    const ov = b.get(k);
    if (!ov || !entriesEqual(v, ov)) return false;
  }
  return true;
}

interface UsePublicTransclusionSyncParams {
  editorViewRef: RefObject<EditorView | null>;
}

export function usePublicTransclusionSync({ editorViewRef }: UsePublicTransclusionSyncParams): void {
  const storeDocuments = useAppStore(s => s.documents);
  const storeReferences = useAppStore(s => s.references);
  const currentDocId = useAppStore(s => s.currentDocument?.document_id);

  // Seed + diff-gated dispatch. Reads the public root doc id from the
  // module-level context (set by PublicSharePage mount) so this hook doesn't
  // take it as a dep — the lazy effect below re-runs whenever the seed deps
  // change. The context gates the public branch (document-keyed endpoints).
  useEffect(() => {
    const publicRootDocId = getPublicFileContext();
    if (!publicRootDocId) return;

    const nextMap = buildPublicTransclusionMap(storeDocuments, storeReferences);
    const nextRefIds = new Set(storeReferences.map(r => r.reference_id));
    const nextDocIds = new Set(storeDocuments.map(d => d.document_id));

    const unchanged = mapsEqual(transcludeMap, nextMap)
      && setsEqual(validRefIds, nextRefIds)
      && setsEqual(validDocIds, nextDocIds);
    if (unchanged) return;

    transcludeMap.clear();
    for (const [k, v] of nextMap) transcludeMap.set(k, v);
    validRefIds.clear();
    for (const id of nextRefIds) validRefIds.add(id);
    validDocIds.clear();
    for (const id of nextDocIds) validDocIds.add(id);

    editorViewRef.current?.dispatch({ effects: linkContextChanged.of(null) });
  }, [storeDocuments, storeReferences, editorViewRef]);

  // Lazy doc-content fetch. Mirrors the authed lazy effect but uses
  // `publicDocumentByDoc(id)` per-id (document-keyed, no batch endpoint on the
  // public router) and reads `currentDocId` to skip the currently-open doc (its
  // content is already in the store and would be overwritten by a fetch racing
  // the load).
  useEffect(() => {
    const view = editorViewRef.current;
    const publicRootDocId = getPublicFileContext();
    if (!view || !publicRootDocId) return;
    let cancelled = false;
    const text = view.state.doc.toString();
    // see SYSTEM: transclusion — id extraction uses the shared `parseTarget` grammar.
    const re = /!\[[^\]]*\]\(([^)]+)\)/g;
    const docIdsToFetch = new Set<string>();
    let m;
    while ((m = re.exec(text)) !== null) {
      const parsed = parseTarget(m[1]);
      if (!parsed) continue;
      if (parsed.scheme === 'ref') continue; // ref bodies are in the store already
      const id = parsed.id;
      if (id === currentDocId) continue; // open doc — store is authoritative
      const entry = transcludeMap.get(id);
      // Only fetch docs that are in scope (validDocIds) and still loading.
      if (!entry || entry.kind !== 'doc' || entry.content !== undefined) continue;
      if (!validDocIds.has(id)) continue;
      docIdsToFetch.add(id);
    }
    if (docIdsToFetch.size === 0) return;
    for (const id of docIdsToFetch) {
      publicDocumentByDoc(id)
        .then((resp) => {
          if (cancelled) return;
          const content = resp.content;
          if (!content || !content.trim()) return; // leave loading rather than seeding empty
          const existing = transcludeMap.get(id);
          if (!existing || existing.kind !== 'doc') return;
          transcludeMap.set(id, { ...existing, content });
          editorViewRef.current?.dispatch({ effects: linkContextChanged.of(null) });
        })
        .catch(() => {
          // WHY no toast: an out-of-scope `doc:` target (sibling moved out of
          // the subtree) 404s and must NOT error the page. Mirrors the authed
          // `catch { /* leave loading */ }` posture.
        });
    }
    return () => { cancelled = true; };
  }, [storeDocuments, storeReferences, currentDocId, editorViewRef]);

  // Clear on unmount: a later authed mount starts from a clean map. The authed
  // `useEditorReferenceSync` re-seeds from its own stores; any stale public
  // entries would render as broken embeds until the authed sync ran.
  useEffect(() => {
    return () => {
      transcludeMap.clear();
      validDocIds.clear();
      validRefIds.clear();
    };
  }, []);
}

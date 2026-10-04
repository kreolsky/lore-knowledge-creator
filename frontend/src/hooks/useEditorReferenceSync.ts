/** Editor reference sync: transclusion map, link-validity sets, unresolved-ref fetch. */
// ARCH: the transclude map and link-validity sets are module-level — CM6 plugins can't access React context.
// ARCH: One diff-gated effect. We compute next contents, compare against current
// module-level state, and dispatch a single `linkContextChanged` only when something
// actually differs. Without the diff, every rename / status flip on any reference
// triggered a full structural-decoration rebuild via livePreviewField.

import { useEffect, useRef, useCallback } from 'react';
import { useAppStore } from '../store/app-store';
import { useNoteChatStore } from '../store/note-chat-store';
import { useEvent } from './useEvent';
import { useTranslation } from '../i18n';
import { apiClient } from '../api/client';
import { fetchContentBatch } from '../api/content-batch';
import { Reference } from '../types';
import { fetchReferences } from '../editor/editor-utils';
import { transcludeMap, linkContextChanged, validDocIds, validNoteThreadIds, validRefIds, projectRefIds, missingRefIds, type TransclusionEntry } from '../components/editor/live-preview';
import { LINK_TYPES } from '../components/editor/live-preview/link-types';
import { parseTarget } from '../components/editor/live-preview/transclusion-grammar';
import { setsEqual } from '../components/editor/live-preview/set-equality';
import { fetchDocumentContent, invalidatePreviewCache, seedDocPreview } from './useDocumentPreview';
import { fetchRefPreview, seedRefPreview } from './useReferencePreview';

// SYSTEM: transclusion — single source of truth for the image-ref URL. Used by both the
// image map and the transclusion map so the two resolution paths never diverge.
function imageRefUrl(ref: Reference): string | null {
  const originalName = ref.file_meta?.original_name;
  return originalName ? `/api/files/${ref.reference_id}/${originalName}` : null;
}

// Value equality for the RENDERED fields of a TransclusionEntry (source is ownership
// metadata, not rendering — excluded so a source-only change never forces a rebuild).
type EntryRender = Pick<TransclusionEntry, 'kind' | 'title' | 'content' | 'imageUrl' | 'fileUrl' | 'fileSize'>;
function entriesEqual(a: EntryRender, b: EntryRender): boolean {
  return a.kind === b.kind
    && a.title === b.title
    && a.content === b.content
    && a.imageUrl === b.imageUrl
    && a.fileUrl === b.fileUrl
    && a.fileSize === b.fileSize;
}
function transcludeMapsEqual(
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

// Carry forward a lazily-fetched body across a metadata-only rebuild.
// ARCH: the LIST (refs) and the document tree (sibling docs) are metadata-only —
// `content` is fetched by the batch/lazy effects and lives ONLY in transcludeMap,
// never written back to the store. The two builder loops below compute entries from
// store metadata; this single transform restores any previously-fetched body so a
// store identity change (optimistic + WS removeReference on delete; prefetch/SWR/network
// mergeReferences on doc switch) never resets a band to LOADING (visible down/up blink).
// Owns the "preserve a fetched body across a metadata rebuild" concept for BOTH refs
// and docs — previously the doc loop carried it inline while the ref loop dropped it.
function carryContent(
  next: TransclusionEntry,
  prev: TransclusionEntry | undefined,
): TransclusionEntry {
  if (next.content !== undefined) return next;            // store carries a fresh body → it wins
  if (prev && prev.kind === next.kind && prev.content !== undefined)
    return { ...next, content: prev.content };            // carry the fetched body forward
  return next;                                             // nothing to carry → LOADING
}

// see SYSTEM: link-types — the ref entry's match is the single source of the
// `[x](ref:id)` grammar (its `!?` also covers labeled embeds `![x](ref:id)`).
const REF_LINK_ENTRY = LINK_TYPES.find((e) => e.kind === 'ref')!;

/** Collect every ref: id present in a text, from BOTH link forms:
 * `[x](ref:id)` links (via the LINK_TYPES ref entry — the same grammar the
 * decorations use) and `![…](ref:id)` embeds including the EMPTY-label form the
 * ref entry's `[^\]]+` label cannot match (via parseTarget, the same grammar the
 * lazy-fetch effect uses). Duplicate ids collapse — the resolver probes SETS. */
function collectRefIdsFromText(text: string): Set<string> {
  const ids = new Set<string>();
  for (const m of REF_LINK_ENTRY.match(text)) ids.add(m.id);
  const embedRe = /!\[[^\]]*\]\(([^)]+)\)/g;
  let m: RegExpExecArray | null;
  while ((m = embedRe.exec(text)) !== null) {
    const parsed = parseTarget(m[1]);
    if (parsed?.scheme === 'ref') ids.add(parsed.id);
  }
  return ids;
}

interface UseEditorReferenceSyncParams {
  editorViewRef: React.RefObject<import('@codemirror/view').EditorView | null>;
}

export function useEditorReferenceSync({ editorViewRef }: UseEditorReferenceSyncParams) {
  const storeReferences = useAppStore(s => s.references);
  const storeDocuments = useAppStore(s => s.documents);
  const noteChatSessions = useNoteChatStore(s => s.sessions);
  const currentDocId = useAppStore(s => s.currentDocument?.document_id);
  const currentProjectIndexId = useAppStore(s => s.currentProject?.index_doc_id);
  const currentProjectId = useAppStore(s => s.currentProject?.project_id);

  // see SYSTEM: transclusion — build a TransclusionEntry for a reference.
  // image refs → ref-image (carries imageUrl, the single source of the image URL);
  // file refs → ref-file (carries fileUrl + fileSize — a download card, never a
  // band); non-image refs with text content → ref-text (markdown body). Returns
  // null when nothing renderable. Callers tag the resulting entry with their own
  // `source`.
  //
  // The LIST endpoint is metadata-only: a text ref whose body isn't loaded yet seeds
  // a LOADING entry (content undefined) when the server reports has_content — so the
  // embed renders a loading band instead of a broken link. A genuinely-empty ref
  // (has_content === false) has nothing to embed → null. has_content undefined falls
  // back to the content check (single-GET/POST refs carry content but no has_content).
  const refToEntry = useCallback((ref: Reference): Omit<TransclusionEntry, 'source'> | null => {
    if (ref.media_type === 'image') {
      const imageUrl = imageRefUrl(ref);
      if (!imageUrl) return null;
      return { kind: 'ref-image', title: ref.title, imageUrl };
    }
    if (ref.media_type === 'file') {
      // WHY require original_name: the download URL is built from it (mirrors the
      // image branch's file_path guard). A file ref without one is a broken save —
      // skip rather than seed a dead download card.
      const originalName = ref.file_meta?.original_name;
      if (!originalName) return null;
      return {
        kind: 'ref-file',
        title: ref.title,
        fileUrl: `/api/files/${ref.reference_id}/${originalName}`,
        fileSize: ref.file_meta?.file_size,
      };
    }
    if (ref.content !== undefined) {
      return ref.content.trim()
        ? { kind: 'ref-text', title: ref.title, content: ref.content }
        : null;
    }
    return ref.has_content === false
      ? null
      : { kind: 'ref-text', title: ref.title };
  }, []);

  // i18n read via a ref — `t` is a NEW function every render and must never enter
  // a dep array (the resolver + content_flushed listeners read it at fire time).
  const { t } = useTranslation();
  const tRef = useRef(t);
  tRef.current = t;

  // ─── project-ref resolver ────────────────────────────────────────────────────
  // ARCH: no code path loads every reference of a project — the project LIST is
  // capped server-side (limit ≤1000), so a project-wide fetch renders refs past the
  // cap as broken. Validity + cross-doc embeds are resolved ONLY for the ref ids that
  // appear in the OPEN document's text: a debounced (300 ms) pass collects them
  // from both link forms, drops ids already known (validRefIds ∪ projectRefIds ∪
  // missingRefIds) and POSTs the rest in chunks of 200 to /references/resolve.
  // Found → projectRefIds + a `project-ref` transcludeMap entry (source ownership
  // unchanged); missing → missingRefIds, so a broken link is not re-asked on every
  // keystroke. A link typed while a pass is pending shows broken for ≤ debounce +
  // RTT — the click recheck (onBrokenLinkClick) still resolves it.
  const resolveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // WHY: one toast per failure streak — a failed pass memoizes nothing, so every
  // editing pause retries, and each retry must not stack another toast.
  const resolveFailedRef = useRef(false);

  const runRefResolve = useCallback(() => {
    const pid = useAppStore.getState().currentProject?.project_id;
    const view = editorViewRef.current;
    if (!pid || !view) return;
    const todo = [...collectRefIdsFromText(view.state.doc.toString())].filter(
      (id) => !validRefIds.has(id) && !projectRefIds.has(id) && !missingRefIds.has(id),
    );
    if (todo.length === 0) return;
    const chunks: string[][] = [];
    for (let i = 0; i < todo.length; i += 200) chunks.push(todo.slice(i, i + 200));
    Promise.all(chunks.map((chunk) =>
      apiClient.post('/references/resolve', { project_id: pid, ids: chunk })
        .then((found: Reference[]) => ({ chunk, found: Array.isArray(found) ? found : [] })),
    )).then((results) => {
      resolveFailedRef.current = false;
      // INVARIANT: a response for a project that is no longer current is dropped.
      // Why: the module Sets are global and were cleared on the switch — writing the
      // old project's verdicts would mark its ids valid/missing in the new one.
      if (useAppStore.getState().currentProject?.project_id !== pid) return;
      // see SYSTEM: transclusion — project-ref writer. Owns the `project-ref` source:
      // seeds cross-document refs (not in the ancestor-scoped storeReferences) so a
      // sibling-doc transclusion resolves like the link-validity path. Read the store
      // imperatively (not via deps) so a reference mutation does not re-trigger this
      // resolver. Replaces only its own source; ancestor-ref/doc entries (owned by
      // the rebuild effect) and content-mutating writes are preserved.
      const ancestorRefIds = new Set(useAppStore.getState().references.map((r) => r.reference_id));
      let validityChanged = false;
      let transclusionChanged = false;
      for (const { chunk, found } of results) {
        const foundIds = new Set(found.map((r) => r.reference_id));
        for (const id of chunk) {
          if (foundIds.has(id)) continue;
          // The server's "absent" verdict — memoized so the id is not re-asked on
          // every keystroke. Also drops a stale project-ref entry (the re-probe a
          // ws:reference_deleted invalidation schedules lands here).
          missingRefIds.add(id);
          const existing = transcludeMap.get(id);
          if (existing?.source === 'project-ref') {
            transcludeMap.delete(id);
            transclusionChanged = true;
          }
        }
        for (const ref of found) {
          projectRefIds.add(ref.reference_id);
          missingRefIds.delete(ref.reference_id);
          validityChanged = true;
          if (ancestorRefIds.has(ref.reference_id)) continue;
          const base = refToEntry(ref);
          if (!base) continue;
          // Carry a previously-fetched body across the re-seed so a re-resolve never
          // flips a loaded band back to LOADING (same contract as the rebuild writer).
          const entry: TransclusionEntry = carryContent(
            { ...base, source: 'project-ref' },
            transcludeMap.get(ref.reference_id),
          );
          const prev = transcludeMap.get(ref.reference_id);
          if (!prev || !entriesEqual(prev, entry)) {
            transcludeMap.set(ref.reference_id, entry);
            transclusionChanged = true;
          }
        }
      }
      if (!validityChanged && !transclusionChanged) return;
      const v = editorViewRef.current;
      if (v) v.dispatch({ effects: linkContextChanged.of(null) });
    }).catch(() => {
      if (resolveFailedRef.current) return;
      resolveFailedRef.current = true;
      useAppStore.getState().showToast(tRef.current('failedToLoadReferences'), 'error');
    });
  }, [refToEntry, editorViewRef]);

  const scheduleRefResolve = useCallback(() => {
    if (resolveTimerRef.current) clearTimeout(resolveTimerRef.current);
    resolveTimerRef.current = setTimeout(() => {
      resolveTimerRef.current = null;
      runRefResolve();
    }, 300);
  }, [runRefResolve]);

  // Project (re)entry: the module Sets are global — reset resolution state so ids
  // resolved for the previous project never leak validity into this one, then run
  // the pass for the open text. Own-source delete of project-ref entries.
  useEffect(() => {
    if (!currentProjectId) {
      projectRefIds.clear();
      missingRefIds.clear();
      return;
    }
    projectRefIds.clear();
    missingRefIds.clear();
    let transclusionChanged = false;
    for (const [id, entry] of transcludeMap) {
      if (entry.source === 'project-ref') {
        transcludeMap.delete(id);
        transclusionChanged = true;
      }
    }
    if (transclusionChanged) {
      const view = editorViewRef.current;
      if (view) view.dispatch({ effects: linkContextChanged.of(null) });
    }
    scheduleRefResolve();
    return () => {
      if (resolveTimerRef.current) clearTimeout(resolveTimerRef.current);
    };
  }, [currentProjectId, scheduleRefResolve, editorViewRef]);

  // Doc open (incl. switches within the same project): probe the new text.
  useEffect(() => {
    if (!currentProjectId || !currentDocId) return;
    scheduleRefResolve();
    return () => {
      if (resolveTimerRef.current) clearTimeout(resolveTimerRef.current);
    };
  }, [currentDocId, currentProjectId, scheduleRefResolve]);

  // Editor doc changes (already debounced upstream by editor-observer) and
  // reference lifecycle invalidations (see useReferenceEvents →
  // 'ref-links-invalidate') re-run the pass.
  useEvent('editor-doc-changed', useCallback(() => {
    scheduleRefResolve();
  }, [scheduleRefResolve]));
  useEvent('ref-links-invalidate', useCallback(() => {
    scheduleRefResolve();
  }, [scheduleRefResolve]));

  // INVARIANT: refs are fetched here (editor owner), not in ReferencesPanel alone.  Why: ReferencesPanel only mounts on the References tab; if refs were fetched only there, a doc switch while on Chat/Notes would leave link validity stale.
  // ReferencesPanel mounts only when the right panel is on the References tab —
  // when the user is on Chat / Notes / any other tab, refs for the new document
  // would never load, leaving link decorations marked as "no reference" in the
  // editor. This effect re-runs on every doc/project change and writes through
  // useAppStore.mergeReferences (idempotent with ReferencesPanel's own fetch).
  useEffect(() => {
    if (currentDocId && currentProjectIndexId) fetchReferences();
  }, [currentDocId, currentProjectIndexId]);

  useEffect(() => {
    const nextTranscludeMap = new Map<string, TransclusionEntry>();
    // Per-source ownership: this rebuild writer owns `ancestor-ref` + `doc`. Preserve every
    // entry it does NOT own (project-ref, seeded by the project-wide effect) so a sibling-doc
    // transclusion survives an ancestor store mutation. Content-mutating writers (lazy-fetch,
    // content_flushed) preserve source too, so a reloaded doc entry is carried as `doc`.
    for (const [k, v] of transcludeMap) {
      if (v.source !== 'ancestor-ref' && v.source !== 'doc') {
        nextTranscludeMap.set(k, v);
      }
    }
    // ancestor-ref: image-ref entries live in the unified transcludeMap (kind:'ref-image',
    // imageUrl) alongside text/doc entries — one resolution path for every embed kind.
    for (const ref of storeReferences) {
      const base = refToEntry(ref);
      if (!base) continue;
      nextTranscludeMap.set(
        ref.reference_id,
        carryContent({ ...base, source: 'ancestor-ref' }, transcludeMap.get(ref.reference_id)),
      );
    }
    // see SYSTEM: transclusion — documents (doc writer). storeDocuments carries content only for
    // the open document (tree metadata omits content for siblings). Seed a loading entry
    // (content undefined) for any doc without content; the async fetch effect below fills
    // content for docs that are actually transcluded. Preserve an already-fetched entry so a
    // keystroke (storeDocuments change) right after a content_flushed reload doesn't wipe the
    // fetched content back to loading (Task 8 × Task 5 ownership contract).
    for (const doc of storeDocuments) {
      const next: TransclusionEntry = doc.content && doc.content.trim()
        ? { kind: 'doc', source: 'doc', title: doc.title, content: doc.content }
        : { kind: 'doc', source: 'doc', title: doc.title };
      nextTranscludeMap.set(
        doc.document_id,
        carryContent(next, transcludeMap.get(doc.document_id)),
      );
    }
    const nextRefIds = new Set(storeReferences.map(r => r.reference_id));
    const nextDocIds = new Set(storeDocuments.map(d => d.document_id));
    const nextNoteIds = new Set(noteChatSessions.map(s => s.session_id));

    // Image entries live inside nextTranscludeMap (kind:'ref-image'), so a single
    // transcludeMapsEqual comparison covers every embed kind — no separate image-map
    // diff term is needed.
    const unchanged = transcludeMapsEqual(transcludeMap, nextTranscludeMap)
      && setsEqual(validRefIds, nextRefIds)
      && setsEqual(validDocIds, nextDocIds)
      && setsEqual(validNoteThreadIds, nextNoteIds);
    if (unchanged) return;

    transcludeMap.clear();
    for (const [k, v] of nextTranscludeMap) transcludeMap.set(k, v);
    validRefIds.clear();
    for (const id of nextRefIds) validRefIds.add(id);
    validDocIds.clear();
    for (const id of nextDocIds) validDocIds.add(id);
    validNoteThreadIds.clear();
    for (const id of nextNoteIds) validNoteThreadIds.add(id);

    const view = editorViewRef.current;
    if (view) view.dispatch({ effects: linkContextChanged.of(null) });
  }, [storeReferences, storeDocuments, noteChatSessions, refToEntry, editorViewRef]);

  // see SYSTEM: transclusion — lazily fetch content for documents AND references that
  // are transcluded but whose entry is still in the loading state (content undefined).
  // Runs debounced after store/link-context changes. Bounded to ids actually present in
  // the editor text, so it never fetches the whole project. All cold ids
  // (docs + text-refs) resolve through ONE POST /documents/batch (fetchContentBatch),
  // whose response also seeds the shared hover-preview caches so a later hover of a
  // batched id is a free cache hit. Resolves both the sibling-doc and content-less-ref
  // cases.
  const docFetchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    const view = editorViewRef.current;
    if (!view) return;
    let cancelled = false;
    if (docFetchTimerRef.current) clearTimeout(docFetchTimerRef.current);
    docFetchTimerRef.current = setTimeout(() => {
      if (cancelled) return;
      const text = view.state.doc.toString();
      // see SYSTEM: transclusion — id extraction uses the shared `parseTarget` grammar.
      const re = /!\[[^\]]*\]\(([^)]+)\)/g;
      const docIds = new Set<string>();
      const refIds = new Set<string>();
      let m;
      while ((m = re.exec(text)) !== null) {
        const parsed = parseTarget(m[1]);
        if (!parsed) continue;
        const id = parsed.id;
        const entry = transcludeMap.get(id);
        if (!entry || entry.content !== undefined) continue; // already loaded
        if (parsed.scheme === 'ref') {
          // ref: target still loading → fetch its body via the preview cache.
          if (entry.kind === 'ref-text') refIds.add(id);
        } else {
          // doc:/bare-doc id still loading — only docs known to the project resolve.
          if (entry.kind === 'doc' && validDocIds.has(id)) docIds.add(id);
        }
      }
      const applyContent = (id: string, content: string | undefined, kind: 'doc' | 'ref-text') => {
        if (cancelled) return;
        if (!content || !content.trim()) return; // leave loading rather than seeding empty
        const existing = transcludeMap.get(id);
        if (!existing || existing.kind !== kind) return;
        // Content-mutating writer: UPDATE in place, preserving the entry's source so the
        // next rebuild (which owns `doc`/`ancestor-ref`) still recognizes it.
        transcludeMap.set(id, { ...existing, content });
        const v = editorViewRef.current;
        if (v) v.dispatch({ effects: linkContextChanged.of(null) });
      };
      // Collapse the two per-id
      // GET loops (N x /documents/{id} + N x /references/{id}, each ~5 serial DB
      // round-trips) into ONE POST /documents/batch. Seeds the per-id preview
      // caches from the response so a later hover-preview of a batched id is a
      // free hit. Best-effort: on failure leave the entries loading.
      const allIds = [...docIds, ...refIds];
      if (allIds.length === 0) return;
      fetchContentBatch(allIds).then((batch) => {
        if (cancelled) return;
        for (const id of allIds) {
          const entry = transcludeMap.get(id);
          if (!entry) continue;
          const isRefText = entry.kind === 'ref-text';
          const preview = batch[id];
          if (preview) {
            if (isRefText) seedRefPreview(id, preview);
            else if (preview.content) seedDocPreview(id, preview.content);
          }
          applyContent(id, preview?.content, isRefText ? 'ref-text' : 'doc');
        }
      }).catch(() => {
        // Fallback to per-id isolation: a single batch failure (rare — network/500,
        // since 404 all-missing is mapped to {} by fetchContentBatch and doesn't
        // reach here) must NOT strand the whole transclusion set. Per-id GETs
        // restore the old granularity and benefit from the in-flight dedup +
        // preview caches. Best-effort: leave loading on per-id failure too.
        if (cancelled) return;
        for (const id of docIds) {
          fetchDocumentContent(id).then((content) => applyContent(id, content, 'doc'))
            .catch(() => { /* leave loading on failure */ });
        }
        for (const id of refIds) {
          fetchRefPreview(id).then((preview) => applyContent(id, preview.content, 'ref-text'))
            .catch(() => { /* leave loading on failure */ });
        }
      });
    }, 300);
    return () => {
      cancelled = true;
      if (docFetchTimerRef.current) clearTimeout(docFetchTimerRef.current);
    };
  }, [storeDocuments, storeReferences, currentDocId, editorViewRef]);

  // Re-fetch references when document contains unresolved ref: image IDs
  const unresolvedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEvent('unresolved-image-ref', useCallback(() => {
    if (unresolvedTimerRef.current) clearTimeout(unresolvedTimerRef.current);
    unresolvedTimerRef.current = setTimeout(fetchReferences, 500);
  }, []));

  // see SYSTEM: transclusion — Bug D: a sibling doc edited in another tab emits content_flushed,
  // which reaches us over the project WS. If that doc is currently shown as a transclusion band
  // (a kind:'doc' entry in transcludeMap), invalidate its preview cache + reload the content so
  // the band stays live. Otherwise early-return (chattiness guard).
  //
  // INVARIANT: content_flushed fires on EVERY debounce flush from many emit sites, so this listener
  // MUST early-return unless entity_id is already in transcludeMap AS A kind:'doc' entry — never
  // fetch on an unrelated flush. Why: avoid N wasted re-fetches per keystroke across all project-WS
  // clients. A transcluded reference lives as kind:'ref-text'/'ref-image', so the kind:'doc' gate
  // skips reference flushes (the payload's is_reference, when the emitter states it, is not
  // consulted — the local map kind is the gate).
  const contentFlushTimerRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  useEvent('ws:content_flushed', useCallback(({ entity_id: entityId, entity_type: entityType }) => {
    if (entityType !== 'doc') return;
    const existing = transcludeMap.get(entityId);
    if (!existing || existing.kind !== 'doc') return; // chattiness guard
    // INVARIANT: never reload the document currently open in THIS editor. Why: the live
    // editor buffer is authoritative and may carry unsaved edits newer than the flushed
    // (saved) snapshot; a server reload would overwrite the band with stale content. The
    // open doc reaches its own band only via a self-transclusion at depth >= 1, which the
    // cycle cap already degrades to a link — so skipping the reload has no visible cost.
    if (entityId === currentDocId) return;
    // Per-entity debounce: a single shared timer would let a later doc's flush cancel an
    // earlier doc's pending reload, leaving that band stale. Key the timer by entity id so
    // coalescing stays per-doc (≤1 reload per doc per 300ms) without starving siblings.
    const timers = contentFlushTimerRef.current;
    const prev = timers.get(entityId);
    if (prev) clearTimeout(prev);
    timers.set(entityId, setTimeout(() => {
      timers.delete(entityId);
      invalidatePreviewCache(entityId);
      fetchDocumentContent(entityId).then((content) => {
        if (!content || !content.trim()) return; // leave the entry rather than seeding empty
        const cur = transcludeMap.get(entityId);
        if (!cur || cur.kind !== 'doc') return;
        // Content-mutating writer: UPDATE in place, preserving the entry's identity/source.
        transcludeMap.set(entityId, { ...cur, content });
        const v = editorViewRef.current;
        if (v) v.dispatch({ effects: linkContextChanged.of(null) });
      }).catch(() => {
        // WHY: no silent degradation. content_flushed fired because the source doc
        // genuinely changed, but the reload failed (network/403). Notify explicitly rather
        // than leaving the band silently showing stale pre-edit content.  Why: the source changed (content_flushed) but the reload failed; silently keeping the stale band would hide the failure, so the user is notified explicitly.
        useAppStore.getState().showToast(tRef.current('transclusionReloadFailed'), 'error');
      });
    }, 300));
  }, [editorViewRef, currentDocId]));
}

import type React from 'react';

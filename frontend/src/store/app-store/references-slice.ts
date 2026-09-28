/** References slice — the reference LIST plus the lazy-hydration of individual bodies.

# ARCH (debt-paydown W6): the last and most entangled app-store slice. It owns the list and
# the two id-sets that mask it (optimistic deletes, in-flight uploads), and the module-level
# hydrate bookkeeping those guards depend on — dedup map + latest-intent id. They live
# together because every one of them exists to answer the same question: which version of a
# reference may be written into the list, and when.
#
# NOT here: `currentReference` / `pendingReference` / `referenceSourceDocId` (entity pointers
# owned by app-store.ts's entity-switch transitions) and `setCurrentDocument` (which commits
# a prefetched ref list as part of the document switch). hydrateReference WRITES the pointer
# through `get().setCurrentReference` rather than owning it — see the latest-wins guard.
*/
import { apiClient } from '../../api/client';
import { t } from '../../i18n';
import type { Reference } from '../../types';
import type { AppState } from '../app-store';

// WHY (hydrate-dedup): concurrent hydrateReference calls for the SAME ref id
// collapse to ONE GET /references/{id}. The shared promise owns the list-refresh +
// currentReference promotion, so a rapid double-open (e.g. restore + card click for
// the same ref) never fires two round-trips. Mirrors the in-flight dedup in
// references-fetch.ts (LIST). Cleared on project switch so a pending GET from the
// previous project can't commit a foreign ref into the new scope.
// Why: see findings hydrate-dedup + id-only-yank — the dedup also makes the
// latest-wins guard below trivially correct for same-id calls.
const hydrateInFlight = new Map<string, Promise<Reference | null>>();
// The id of the most-recently-STARTED hydrate. Promotion of a resolved fetch is
// gated on this so a fetch that settles AFTER a later ref/doc click never yanks
// currentReference back — uniform across the in-store (marker) and id-only paths.
// Cleared on project switch (clearHydrateInFlight) together with the in-flight map.
let hydrateLastId: string | null = null;

// Re-exported from app-store.ts: app-store.test.ts imports this name from the store module
// (11 call sites) and the project-switch reset calls it. Kept as a named export on BOTH
// modules so the import surface did not move with the implementation.
export function clearHydrateInFlight(): void {
  hydrateInFlight.clear();
  hydrateLastId = null;
}

// WHY: commitRefList is the SINGLE place that decides whether deletedRefIds is
// Why: centralizing the filter keeps optimistic deletes from being resurrected by a server snapshot.
// applied when writing the `references` list. honorDeleted is explicit per caller:
//   mergeReferences -> true  (a server snapshot must not resurrect a ref the user just
//                             optimistically deleted, before its DELETE round-trips)
//   setReferences / addReference / removeReference -> false (direct assignment and
//                             save-flow rollback must NOT be hidden by the deleted-set,
//                             or a rolled-back ref would silently vanish)
// Why: the filter used to be inlined in mergeReferences only, so a new full-list entry
// point could silently skip it (or wrongly apply it). Centralizing makes the divergence
// a visible, intentional flag instead of a scattered convention one refactor away from
// breaking.
export function commitRefList(
  refs: Reference[], honorDeleted: boolean, deletedRefIds: Set<string>,
): Reference[] {
  return honorDeleted ? refs.filter(r => !deletedRefIds.has(r.reference_id)) : refs;
}

// Field-preserving per-ref merge of an incoming metadata-only LIST against the existing
// (possibly-hydrated) list. WHY: keyed on `has_content`, NOT "field absent".
//   - has_content === false → genuinely emptied: overwrite content to '' (never show
//     stale content as current — no-silent-degradation). Why: has_content is the authoritative server signal; "field absent" is ambiguous between omitted and emptied.
//   - has_content === true + content-less incoming + store has a hydrated body → PRESERVE
//     content/headings so a lazy-fetched body survives a metadata-only refetch.
//   - otherwise → incoming row is authoritative.
// Shared by mergeReferences (panel refetch) AND setCurrentDocument's doc-switch commit
// (so refs shared across docs — index/ancestor refs are in every scope — keep their
// hydrated bodies instead of being wiped to content-less on every switch).
export function mergeRefLists(incoming: Reference[], existing: Reference[]): Reference[] {
  const hydrated = new Map(existing.map(r => [r.reference_id, r]));
  return incoming.map((inc) => {
    const ex = hydrated.get(inc.reference_id);
    if (!ex) return inc;
    if (inc.has_content === false) return { ...inc, content: '' };
    if (inc.content === undefined && ex.content !== undefined) {
      return { ...inc, content: ex.content, headings: ex.headings };
    }
    return inc;
  });
}

// Hydrated bodies of refs that LEFT the list on a doc switch (a ref owned by doc A is not
// in doc B's scope), keyed by id. Insertion-ordered Map, capped as a FIFO.
// INVARIANT(no-silent-degradation): a stashed body is restored only onto a row whose
// updated_at equals the one it was fetched with. Why: every content save bumps
// updated_at, while a collab edit by another user emits no per-reference event — the
// version match is the only proof the body is still current.
const droppedBodies = new Map<string, Reference>();
const DROPPED_BODIES_MAX = 50;

export function clearDroppedBodies(): void {
  droppedBodies.clear();
}

/** Stash the hydrated rows of `prev` that are absent from `next`. */
export function stashDroppedBodies(prev: Reference[], next: Reference[]): void {
  const kept = new Set(next.map(r => r.reference_id));
  for (const r of prev) {
    if (kept.has(r.reference_id) || r.content === undefined) continue;
    droppedBodies.delete(r.reference_id);
    droppedBodies.set(r.reference_id, r);
  }
  while (droppedBodies.size > DROPPED_BODIES_MAX) {
    droppedBodies.delete(droppedBodies.keys().next().value!);
  }
}

/** Fill content-less incoming rows from the stash when the version still matches. */
export function restoreDroppedBodies(incoming: Reference[]): Reference[] {
  return incoming.map((inc) => {
    const stashed = droppedBodies.get(inc.reference_id);
    if (!stashed || inc.content !== undefined || inc.has_content === false) return inc;
    if (stashed.updated_at !== inc.updated_at) return inc;
    return { ...inc, content: stashed.content, headings: stashed.headings };
  });
}

export type ReferencesSlice = Pick<
  AppState,
  | 'references'
  | 'referencesReloadKey'
  | 'deletedRefIds'
  | 'pendingUploadRefIds'
  | 'setReferences'
  | 'mergeReferences'
  | 'addReference'
  | 'removeReference'
  | 'updateReference'
  | 'replaceReference'
  | 'bumpReferencesReload'
  | 'addRefToDeleting'
  | 'removeRefFromDeleting'
  | 'addPendingUploadRefIds'
  | 'removePendingUploadRefIds'
  | 'hydrateReference'
>;

type AppSet = (
  partial: Partial<AppState> | ((state: AppState) => Partial<AppState> | AppState),
) => void;
type AppGet = () => AppState;

export function createReferencesSlice(set: AppSet, get: AppGet): ReferencesSlice {
  return {
    references: [],
    referencesReloadKey: 0,
    deletedRefIds: new Set<string>(),
    pendingUploadRefIds: new Set<string>(),

    setReferences: (refs) => set(({ deletedRefIds }) => ({
      references: commitRefList(refs, false, deletedRefIds),
    })),
    mergeReferences: (refs) => set(({ references, deletedRefIds }) => ({
      references: commitRefList(mergeRefLists(refs, references), true, deletedRefIds),
    })),
    addReference: (ref) => set(({ references, deletedRefIds }) => {
      const filtered = references.filter(r => r.reference_id !== ref.reference_id);
      // Re-adding an id that is pending deletion cancels the delete — otherwise
      // commitRefList would mask the ref the user just recreated.
      const nextDeleted = deletedRefIds.has(ref.reference_id)
        ? (() => { const s = new Set(deletedRefIds); s.delete(ref.reference_id); return s; })()
        : deletedRefIds;
      return { references: commitRefList([ref, ...filtered], false, nextDeleted), deletedRefIds: nextDeleted };
    }),
    removeReference: (id) => set(({ references, deletedRefIds }) => ({
      references: commitRefList(references.filter(r => r.reference_id !== id), false, deletedRefIds),
    })),
    updateReference: (id, patch) => set(({ references }) => ({
      references: references.map(r => r.reference_id === id ? { ...r, ...patch } : r),
    })),
    replaceReference: (tempId, ref) => set(({ references }) => {
      const without = references.filter(r => r.reference_id !== tempId);
      if (without.some(r => r.reference_id === ref.reference_id)) return { references: without };
      return { references: [ref, ...without.filter(r => r.reference_id !== ref.reference_id)] };
    }),
    // Bump a nonce the References panel watches so it re-fetches its per-host
    // list when a reference moves into/out of the currently-shown document.
    bumpReferencesReload: () => set(({ referencesReloadKey }) => ({
      referencesReloadKey: referencesReloadKey + 1,
    })),

    // Both deleting-set mutators return {} (no state change) when the id is already in the
    // wanted state. Why: a new Set identity on a no-op re-renders every panel subscribed to
    // deletedRefIds during a multi-select delete.
    addRefToDeleting: (id) => set(({ deletedRefIds }) => {
      if (deletedRefIds.has(id)) return {};
      const next = new Set(deletedRefIds);
      next.add(id);
      return { deletedRefIds: next };
    }),
    removeRefFromDeleting: (id) => set(({ deletedRefIds }) => {
      if (!deletedRefIds.has(id)) return {};
      const next = new Set(deletedRefIds);
      next.delete(id);
      return { deletedRefIds: next };
    }),

    addPendingUploadRefIds: (ids) => set((state) => {
      const next = new Set(state.pendingUploadRefIds);
      ids.forEach(id => next.add(id));
      return { pendingUploadRefIds: next };
    }),
    removePendingUploadRefIds: (ids) => set((state) => {
      const next = new Set(state.pendingUploadRefIds);
      ids.forEach(id => next.delete(id));
      return { pendingUploadRefIds: next };
    }),

    hydrateReference: async (refOrId) => {
      const id = typeof refOrId === 'string' ? refOrId : refOrId.reference_id;
      const existing = typeof refOrId === 'string'
        ? get().references.find(r => r.reference_id === id) ?? null
        : refOrId;
      // Whether a loading marker was committed (in-store ref). "In-store" means
      // the LIST carries the id — NOT the argument shape: a restore stub
      // ({ reference_id }-only, panel quick preview of a cross-doc ref) is an
      // object but foreign to the list, so it must take the id-only path
      // (replaceReference adds the fetched row; hydrateLastId promotion guard).
      const hadExisting = typeof refOrId === 'string'
        ? !!existing
        : get().references.some(r => r.reference_id === id);

      // Commit the list ref immediately so a content-less ref shows the editor's loading
      // state (content === undefined && has_content) rather than the prior entity during
      // the fetch. No-op visually for already-hydrated / image refs (committed verbatim).
      if (existing) get().setCurrentReference(existing);

      // Image refs carry no text body; an already-hydrated ref needs no fetch.
      if (existing && (existing.media_type === 'image' || existing.content !== undefined)) {
        return existing;
      }

      // WHY (hydrate-dedup): a concurrent hydrate for the same id shares the
      // in-flight promise (one GET). Still record latest-intent so the shared resolution
      // promotes only if this id is still the most recent request.
      hydrateLastId = id;
      const cached = hydrateInFlight.get(id);
      if (cached) return cached;

      // Capture the project at fetch start — a switch mid-GET must never commit a ref
      // from the previous project into the new scope (references are cleared on switch).
      // Raw optional value (no `?? null`): currentProject?.project_id is `undefined` when
      // no project is loaded yet (cold F5); the post-GET compare treats that undefined start
      // as "scope not yet established" and lets any later id promote (see INVARIANT below).
      const startProj = get().currentProject?.project_id;
      const p = (async (): Promise<Reference | null> => {
        try {
          const full = await apiClient.get(`/references/${id}`) as Reference;
          const s = get();
          // INVARIANT (ref-scope): promote a hydrated ref only if the project didn't switch to
          // a DIFFERENT project mid-GET; a start value of undefined (project not yet loaded on
          // cold F5) matches any later id. Why: bare `!== startProj` mistook cold-load project
          // population for a switch and stranded the ref on "Loading…" forever.
          if (startProj !== undefined && s.currentProject?.project_id !== startProj) return full;
          // Refresh the list entry within the same project. In-store: an in-place
          // updateReference preserves the row's position (no panel reorder / full rebuild
          // on every hydrate). Id-only (cross-doc ref not in this scope): replaceReference
          // adds it, matching the prior behaviour of surfacing an opened foreign ref.
          if (hadExisting) s.updateReference(id, full);
          else s.replaceReference(id, full);
          // Latest-wins guard, path-aware:
          //   - in-store (marker committed): the selection must STILL be this ref — a
          //     non-hydrate navigation away (back button, doc open, collab null, chat
          //     source) sets currentReference to something else and must NOT be yanked back.
          //     `currentReference?.reference_id === id` observes the ACTUAL selection
          //     (hydrateLastId alone misses these — it's only bumped inside hydrateReference).
          //   - id-only (no marker): hydrateLastId === id is the best-available signal
          //     (there's no marker to observe); catches ref→ref navigation, matching the
          //     pre-existing id-only contract.
          // Both paths also require hydrateLastId === id so a later hydrate-initiated open
          // (in-store or id-only) supersedes an earlier one.
          const stillSelected = hadExisting
            ? (s.currentReference?.reference_id === id && hydrateLastId === id)
            : hydrateLastId === id;
          if (stillSelected) s.setCurrentReference(full);
          return full;
        } catch (err) {
          console.error(`Failed to hydrate reference ${id}:`, err);
          get().showToast(t('failedToFetchReference'), 'error');
          return null;
        }
      })();
      hydrateInFlight.set(id, p);
      p.finally(() => { if (hydrateInFlight.get(id) === p) hydrateInFlight.delete(id); });
      return p;
    },
  };
}

/** Ghost chat context: the DERIVED context for a chat panel with no session yet. */
// SYSTEM: ghost-context — the DERIVED context for a chat panel with no session yet.
// ARCH: a ghost (activeSessionId === null) has no stored bucket — its context is a
// PURE function of the open entity + first-circle + manual deltas (see
// deriveGhostContext below), recomputed on every read. The GHOST_SESSION_ID sentinel
// (kept in ./context, where the materialized-session bucket machinery uses it) is
// used ONLY as a branch key: the content picker toggles fold manual deltas
// (add/removeGhostDelta) when the active session id is that sentinel, and the picker
// display reads the derived value (useGhostChatContext). At materialization
// (createSession) the derived value is snapshotted onto the real id via
// setContextForSession — there is no bucket transfer.

import { create } from 'zustand';
import { addWithCascade, subtractWithCascade, type FirstCircle } from '../utils/cascade-selection';

// ─────────────────────────────────────────────────────────────────────────────
// Ghost context: DERIVED (race-free by construction).
// ─────────────────────────────────────────────────────────────────────────────
// INVARIANT(persisted): ghost (zero-chat) auto-context is NEVER stored. It is a pure function
// of (currentDocument, currentReference, showsBothPanes(refOpenMode), linkCache, manual deltas),
// recomputed on every read. Why: a STORED + imperative + async-
// merge ghost bucket was the entire bug class (parent leaks into agent prompt,
// ref→parent plaque, over-inclusion race, ref-open-then-chat-tab staleness,
// post-refresh parent context). One rule, three duplicated implementations, five
// order-dependent async triggers — derive-from-scratch has no writers to race.
// Only the ghost is derived; a materialized session keeps its stored snapshot
// (materialization: snapshot deriveGhostContext once at createSession, then normal
// picker writes + PATCH + rollback).
// The `currentReference` INPUT is the refIsScope-projected one (use-ghost-context
// reactive path, chat-store bridge snapshot path): a panel-mode quick previewed
// reference arrives here as null, so "what's open" means "what's open AS THE
// SCOPE" — the doc. The mode itself needs no tuple slot: the projection flips
// refId null↔id, which every existing key (baseKey, memo deps) already observes.

export interface GhostDeltas {
  addedDocIds: string[];
  addedRefIds: string[];
  removedDocIds: string[];
  removedRefIds: string[];
}

export interface GhostDeriveInput {
  docId: string | null;
  refId: string | null;
  splitActive: boolean;
  linkCache: ReadonlyMap<string, FirstCircle>;
  deltas: GhostDeltas;
}

const EMPTY_FIRST_CIRCLE: FirstCircle = Object.freeze({ document_ids: [], reference_ids: [] });

/**
 * SINGLE SOURCE OF TRUTH for "which open-entity ids form the ghost base". Consumed
 * by deriveGhostContext (fold + first-circle), computeGhostBaseKey (delta-reset
 * identity), and useGhostContextWarm (which ids to fetch first-circle for). All
 * three MUST agree — extracting this helper prevents drift (a base rule change in
 * one site leaving another silently un-warmed / mis-keyed).
 *
 * base is UNCONDITIONAL — the open entity (doc / ref / split panes) always counts
 * as context; nothing must gate it out (see the INVARIANT on deriveGhostContext).
 */
export function ghostBaseTargets(
  docId: string | null,
  refId: string | null,
  splitActive: boolean,
): { docs: string[]; refs: string[] } {
  const docs: string[] = [];
  const refs: string[] = [];
  if (splitActive) {
    if (docId) docs.push(docId);
    if (refId) refs.push(refId);
  } else if (refId) {
    refs.push(refId);
  } else if (docId) {
    docs.push(docId);
  }
  return { docs, refs };
}

/**
 * Pure ghost-context selector. base = ghostBaseTargets (what's open, UNCONDITIONAL)
 * + first-circle from `linkCache` (bare id only when cold); then ⊕ manual deltas via
 * the symmetric addWithCascade/subtractWithCascade helpers. Order-independent and
 * side-effect free — call it twice with the same inputs, get the same result. No
 * stored bucket, no merge, no epoch → race-free by construction.
 *
 * INVARIANT (the open entity is ALWAYS in the base — never gate it out): the
 * PRE-refactor attachOpenEntityToGhost attached the open entity UNCONDITIONALLY,
 * and the send path read that STORED context — so the open doc was ALWAYS in
 * context. Gating the derived base on any flag would silently drop a plain open
 * doc out of context (the exact regression the refactor exists to prevent). The
 * old write-only setTalkToDocument flag (removed) proved this: it had zero
 * readers precisely because the base was never conditional on it. Why: keep the
 * observed behavior — whatever is open on screen is the ghost's context.
 */
export function deriveGhostContext(input: GhostDeriveInput): { docIds: string[]; refIds: string[] } {
  const base = ghostBaseTargets(input.docId, input.refId, input.splitActive);

  // Fold base + first-circle + manual deltas. First-circle is read from the warm
  // cache only (a missing entry contributes the bare id; the warm fetch fills the
  // cache, whose version bump (api/links.ts) makes the selector recompute).
  const linksFor = (kind: 'doc' | 'ref', id: string): FirstCircle =>
    input.linkCache.get(`${kind}:${id}`) ?? EMPTY_FIRST_CIRCLE;

  let sel = { docIds: [] as string[], refIds: [] as string[] };
  for (const id of base.docs) sel = addWithCascade(sel, 'doc', id, linksFor('doc', id));
  for (const id of base.refs) sel = addWithCascade(sel, 'ref', id, linksFor('ref', id));
  for (const id of input.deltas.addedDocIds) sel = addWithCascade(sel, 'doc', id, linksFor('doc', id));
  for (const id of input.deltas.addedRefIds) sel = addWithCascade(sel, 'ref', id, linksFor('ref', id));
  for (const id of input.deltas.removedDocIds) sel = subtractWithCascade(sel, 'doc', id, linksFor('doc', id));
  for (const id of input.deltas.removedRefIds) sel = subtractWithCascade(sel, 'ref', id, linksFor('ref', id));

  return { docIds: sel.docIds, refIds: sel.refIds };
}

/**
 * baseKey = the identity of the ghost's BASE (what's open), derived from the SAME
 * ghostBaseTargets the selector uses — NOT the raw "docId|refId|split" tuple. Why:
 * in non-split-with-ref mode the base is the ref only (doc ignored), so switching
 * the background document must NOT change the baseKey (and must NOT reset manual
 * deltas, which were relative to the unchanged ref). Keying on the actual base
 * keeps "what changed" == "what the base was".
 */
export function computeGhostBaseKey(
  docId: string | null,
  refId: string | null,
  splitActive: boolean,
): string {
  const b = ghostBaseTargets(docId, refId, splitActive);
  return `${b.docs.join(',')}|${b.refs.join(',')}`;
}

// Tiny ephemeral store: ghost-only manual deltas + the current base key. Reactivity on
// a warm first-circle fetch is NOT tracked here — it lives at the cache's source
// (api/links.ts version + subscribeLinkCache), which useGhostChatContext subscribes to.
export interface GhostDeltaState extends GhostDeltas {
  baseKey: string;
}

const useGhostDeltaStore = create<GhostDeltaState>(() => ({
  addedDocIds: [],
  addedRefIds: [],
  removedDocIds: [],
  removedRefIds: [],
  baseKey: '',
}));

/**
 * Reactive snapshot of the whole ghost-delta state for the derive/warm hooks.
 * Returns the FULL state object (not a fresh sub-object): in zustand v5 a selector
 * returning a new object literal each call is an unstable useSyncExternalStore
 * snapshot → infinite render loop. The whole state is referentially stable across
 * renders (replaced only on setState), so it is a safe snapshot. Re-renders fire on
 * any change to deltas / baseKey — the warm-fetch reactivity is a separate subscription
 * (subscribeLinkCache) that useGhostChatContext reads alongside this snapshot.
 */
export function useGhostDeltaSnapshot(): GhostDeltaState {
  return useGhostDeltaStore();
}

/** Non-reactive delta read (for the synchronous materialization snapshot). */
export function getGhostDeltas(): GhostDeltas {
  const s = useGhostDeltaStore.getState();
  return {
    addedDocIds: s.addedDocIds,
    addedRefIds: s.addedRefIds,
    removedDocIds: s.removedDocIds,
    removedRefIds: s.removedRefIds,
  };
}

/**
 * Sync the ghost base key to the currently-open entity. When it changes, RESET the
 * manual deltas — they were relative to the old base. Why: the ghost's
 * context IS what's open on screen; a stale delta from a previous entity must not
 * survive an entity switch. Matches the prior clear-and-reattach behavior.
 *
 * Returns the SAME state object when nothing changed (no baseKey change) and reuses
 * the existing empty array refs when the deltas are already empty — both avoid
 * spurious reference swaps that would needlessly re-run the warm effect (and re-fire
 * its fetches) on mount / an entity switch that leaves the base identity unchanged.
 */
export function syncGhostBaseKey(key: string): void {
  useGhostDeltaStore.setState(s => {
    if (s.baseKey === key) return s;
    // baseKey changed → clear deltas, reusing refs where already empty (no churn).
    return {
      baseKey: key,
      addedDocIds: s.addedDocIds.length ? [] : s.addedDocIds,
      addedRefIds: s.addedRefIds.length ? [] : s.addedRefIds,
      removedDocIds: s.removedDocIds.length ? [] : s.removedDocIds,
      removedRefIds: s.removedRefIds.length ? [] : s.removedRefIds,
    };
  });
}

/** Manual add on a ghost: fold +id (and its cached first-circle) into the derived value. */
export function addGhostDelta(kind: 'doc' | 'ref', id: string): void {
  useGhostDeltaStore.setState(s => {
    if (kind === 'doc') {
      return {
        addedDocIds: s.addedDocIds.includes(id) ? s.addedDocIds : [...s.addedDocIds, id],
        removedDocIds: s.removedDocIds.filter(x => x !== id),
      };
    }
    return {
      addedRefIds: s.addedRefIds.includes(id) ? s.addedRefIds : [...s.addedRefIds, id],
      removedRefIds: s.removedRefIds.filter(x => x !== id),
    };
  });
}

/** Manual remove on a ghost: drop +id (and its cached first-circle) from the derived value. */
export function removeGhostDelta(kind: 'doc' | 'ref', id: string): void {
  useGhostDeltaStore.setState(s => {
    if (kind === 'doc') {
      return {
        removedDocIds: s.removedDocIds.includes(id) ? s.removedDocIds : [...s.removedDocIds, id],
        addedDocIds: s.addedDocIds.filter(x => x !== id),
      };
    }
    return {
      removedRefIds: s.removedRefIds.includes(id) ? s.removedRefIds : [...s.removedRefIds, id],
      addedRefIds: s.addedRefIds.filter(x => x !== id),
    };
  });
}

/** Reset all ghost deltas (materialization). Keeps baseKey. */
export function resetGhostDeltas(): void {
  useGhostDeltaStore.setState({
    addedDocIds: [],
    addedRefIds: [],
    removedDocIds: [],
    removedRefIds: [],
  });
}

/** Test-only: full reset of the ghost delta store (incl. baseKey + version). */
export function __resetGhostDeltaStore(): void {
  useGhostDeltaStore.setState({
    addedDocIds: [],
    addedRefIds: [],
    removedDocIds: [],
    removedRefIds: [],
    baseKey: '',
  });
}

/**
 * Pinned-region persistence (frontend-owned).
 *
 * // SYSTEM: pending-selection — localStorage map of session_id -> pinned region.
 *
 * The backend stores ONLY `chat_sessions.has_region`. The Yjs RelativePosition
 * pair that tracks the region across edits is frontend-owned and persisted here
 * (keyed by session_id) so a same-device reload restores the pin. Cross-device
 * has no RelativePosition → the backend's fail-closed apply gate rejects edits
 * until the user re-pins or unpins (documented limitation).
 *
 * v2 supersedes the removed v1 (lore.agent.pendingSelectionBySession.v1, cleared
 * one-shot in chat-store.ts). The storage shape is JSON: { sessionId: PinnedRegion }.
 */
import type { PinnedRegion, RegionRef } from '../../types';
import * as Y from 'yjs';
import { getActiveHandle } from '../../editor/active-editor';
import { utf16ToCp } from '../../utils/utf16-cp';

const STORAGE_KEY = 'lore.agent.regionBySession.v2';

// ARCH: in-memory mirror of the parsed localStorage map, invalidated only
// by setPendingRegion / clearPendingRegion (the two writers). getPendingRegion is on
// the hot path — the region-highlight StateField calls it on every doc-changed
// transaction — so a per-call localStorage.getItem + JSON.parse (the old behavior) was
// needless sync I/O + parsing. The mirror makes the read O(1). Lazy-filled on first
// access; if localStorage changes out-of-band (another tab) the worst case is a stale
// read until the next set/clear here, which is acceptable for a UI highlight cache.
let _mapCache: Record<string, PinnedRegion> | null = null;

function readMap(): Record<string, PinnedRegion> {
  if (_mapCache !== null) return _mapCache;
  let map: Record<string, PinnedRegion> = {};
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed: unknown = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') {
        map = parsed as Record<string, PinnedRegion>;
      }
    }
  } catch (e) {
    // WHY: log only — localStorage is a per-browser convenience (blocked in private
    // windows); the empty map is the correct state when it cannot be read.
    console.debug('pending-region read skipped', e);
  }
  _mapCache = map;
  return map;
}

function writeMap(map: Record<string, PinnedRegion>): void {
  _mapCache = map;  // mirror the write so the next read is an O(1) memory hit
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch (e) {
    // WHY: log only — the in-memory cache above already holds the write for this tab.
    console.debug('pending-region write skipped', e);
  }
}

export function setPendingRegion(sessionId: string, region: PinnedRegion): void {
  const map = { ...readMap() };
  map[sessionId] = region;
  writeMap(map);
}

export function getPendingRegion(sessionId: string): PinnedRegion | null {
  return readMap()[sessionId] ?? null;
}

export function clearPendingRegion(sessionId: string): void {
  const map = readMap();
  if (sessionId in map) {
    const next = { ...map };
    delete next[sessionId];
    writeMap(next);
  }
}

/**
 * Resolve a PinnedRegion's Yjs RelativePosition pair to a UTF-16 [from, to] range
 * against the FOCUSED editor's live ydoc. Returns null when there is no live ydoc
 * (offline / not-yet-bound) OR the anchor no longer resolves (lost after a wholesale
 * restore/import). The single region→range resolver: consumed by the editor highlight
 * (Editor.regionResolver), the SelectionPill preview, and resolveRegion below (which
 * layers the offline-vs-lost distinction + code-point conversion on top). Extracting
 * it keeps one RelativePosition→absolute implementation (normalize the class, not the
 * symptom) instead of four drifting copies.
 */
export function resolveRegionRange(region: PinnedRegion): RegionRange | null {
  if (!region.relFrom || !region.relTo) return null;
  const ydoc = getActiveHandle()?.ydoc;
  if (!ydoc) return null;
  try {
    const fromAbs = Y.createAbsolutePositionFromRelativePosition(
      Y.createRelativePositionFromJSON(region.relFrom as object), ydoc,
    );
    const toAbs = Y.createAbsolutePositionFromRelativePosition(
      Y.createRelativePositionFromJSON(region.relTo as object), ydoc,
    );
    if (!fromAbs || !toAbs) return null; // anchor gone (wholesale restore/import)
    return { from: fromAbs.index, to: toAbs.index };
  } catch {
    return null;
  }
}

export interface RegionRange {
  from: number;
  to: number;
}

/**
 * Discriminated outcome of resolving a pinned region against the live ydoc. The
 * caller MUST distinguish `offline` from `lost`:
 *  - `offline` (no live ydoc) is a transient cross-device/reload condition — KEEP the
 *    pin; the backend apply gate fails closed until the same-device editor rebinds.
 *  - `lost` (ydoc present, anchor unresolvable → null) means the anchoring client's
 *    ops are ABSENT from the live doc: a server-side ydoc-log compaction dropped the
 *    tombstones the RelativePosition needs, a cross-device reload diverged, or a
 *    foreign-client restore. (Note: a SAME-doc wholesale replace tombstones in place
 *    and COLLAPSES the anchor to a boundary — that surfaces as `ok` with from==to and
 *    is handled by the collapsed-region UX, not here.) A pin over a null anchor is
 *    dead weight — it forces confirm and rejects every apply forever — so the caller
 *    auto-unpins + tells the user to re-pin.
 */
export type RegionResolution =
  | { status: 'ok'; region: RegionRef }
  | { status: 'none' }     // nothing pinned for this session
  | { status: 'offline' }  // no live ydoc (cross-device / not yet bound) — keep the pin
  | { status: 'lost' };    // ydoc present, anchor gone (wholesale restore/import)

/**
 * Resolve a session's pinned region against the FOCUSED editor's live ydoc.
 *
 * A collapsed region (from==to after user deletions) resolves to `ok` with
 * from_cp==to_cp — the backend apply gate rejects it as out-of-scope; that is a
 * distinct (recoverable) state from `lost`, so it is NOT auto-unpinned here.
 *
 * // INVARIANT: resolution runs against the SAME ydoc the editor binds (the focused
 * // handle) so the offsets reflect the CRDT-converged text the agent will edit.  Why: the pin resolution must use the editor's bound ydoc so offsets match the CRDT-converged text the agent edits (no stale-text mismatch).
 */
export function resolveRegion(sessionId: string): RegionResolution {
  const region = getPendingRegion(sessionId);
  if (!region || !region.relFrom || !region.relTo) return { status: 'none' };
  const handle = getActiveHandle();
  const ydoc = handle?.ydoc;
  const ytext = handle?.ytext;
  // Offline (no live ydoc) is distinct from lost (ydoc present, anchor null): only the
  // latter auto-unpins. resolveRegionRange collapses both to null, so keep the offline
  // guard HERE (before the helper) — a null from the helper with a live ydoc is 'lost'.
  if (!ydoc || !ytext) return { status: 'offline' };
  const range = resolveRegionRange(region);
  // ydoc present but the anchor did not resolve (or a malformed RelativePosition threw
  // inside the helper) → lost, not offline: an unrecoverable anchor must not freeze the
  // session forever.
  if (!range) return { status: 'lost' };
  const full = ytext.toString();
  const from_cp = utf16ToCp(full, range.from);
  const to_cp = utf16ToCp(full, range.to);
  const text = full.slice(range.from, range.to);
  return { status: 'ok', region: { doc_id: region.doc_id, from_cp, to_cp, text } };
}

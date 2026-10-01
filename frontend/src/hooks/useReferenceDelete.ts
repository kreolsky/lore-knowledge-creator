/** Batched soft-delete for references — collects rapid deletions and flushes one request.
 *
 * Why a hook: replaces two divergent inline-DELETE implementations (ReferencesPanel,
 * ReferenceViewerBanner) with a single batched mechanism. Rapid bulk deletes from the
 * UI no longer fan out N concurrent DELETEs that saturate the shared SurrealDB
 * connection.
 */
// ARCH: Single-in-flight + size-or-idle batching. `scheduleDelete` accumulates IDs in
// `pendingRef`; flush is triggered either when ≥ BATCH_SIZE_THRESHOLD IDs are queued or
// after FLUSH_DELAY_MS of idle. Only one POST may be in flight (inFlightRef); concurrent
// scheduleDelete calls during a flight pile up and are sent in the next batch as soon
// as the current POST resolves. This avoids saturating the browser's 6-connection
// concurrency budget and the shared SurrealDB connection, both of which previously
// caused per-click POSTs to take 5–11 s and be stranded on F5.
//
// On page unload `navigator.sendBeacon` delivers any remaining pending IDs so a refresh
// during bulk delete cannot strand rows in the DB. Rollback on error restores all
// batched refs.
// SYSTEM: reference-delete — single-in-flight batched deletion with beacon-on-unload guarantee

import { useEffect, useRef, useCallback } from 'react';
import { apiClient } from '../api/client';
import { useAppStore } from '../store/app-store';
import type { Reference } from '../types';

const FLUSH_DELAY_MS = 800;
const BATCH_SIZE_THRESHOLD = 5;
// INVARIANT: deletedRefIds must outlive any in-flight GET /references that started  Why: an in-flight GET started before the delete reaches the DB would re-fetch deleted refs; deletedRefIds outlives that race and filters them until the WS event confirms.
// before the POST batch-delete reached the DB. Primary cleanup path is the WS
// `ws:reference_deleted` event (useReferenceEvents) — it clears the entry as
// soon as the backend confirms. This timer is the fallback for the case where the
// WS connection is degraded or the event is dropped: under slow networks an
// in-flight GET could return the row before backend wrote deleted_at; without the
// fallback, mergeReferences would resurrect the deleted ref. 10s covers typical
// REST round-trips on degraded links.
const DELETED_GRACE_MS = 10_000;
const BATCH_ENDPOINT = '/references/batch-delete';

export function useReferenceDelete() {
  const pendingRef = useRef<Map<string, Reference>>(new Map());
  const flushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inFlightRef = useRef(false);

  const flush = useCallback(async () => {
    if (flushTimerRef.current) {
      clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    if (inFlightRef.current) return;
    if (pendingRef.current.size === 0) return;
    const snapshot = new Map(pendingRef.current);
    pendingRef.current.clear();
    const ids = Array.from(snapshot.keys());
    inFlightRef.current = true;
    try {
      await apiClient.post(BATCH_ENDPOINT, { reference_ids: ids });
      // Grace period: any GET /references that started before the POST may still be
      // in flight and return rows whose deleted_at hadn't been written yet. Keep them
      // in deletedRefIds so mergeReferences filters them out. The WS event
      // `ws:reference_deleted` clears the entry sooner (useReferenceEvents).
      setTimeout(() => {
        const store = useAppStore.getState();
        ids.forEach(id => store.removeRefFromDeleting(id));
      }, DELETED_GRACE_MS);
    } catch {
      const store = useAppStore.getState();
      snapshot.forEach((ref, id) => {
        store.removeRefFromDeleting(id);
        store.addReference(ref);
      });
      store.showToast('Failed to delete references', 'error');
    } finally {
      inFlightRef.current = false;
      // Chain: anything accumulated while we were in flight goes out as the next batch.
      if (pendingRef.current.size > 0) flush();
    }
  }, []);

  const scheduleDelete = useCallback((ref: Reference) => {
    pendingRef.current.set(ref.reference_id, ref);
    useAppStore.getState().addRefToDeleting(ref.reference_id);

    // Size trigger: as soon as ≥ threshold IDs are queued and nothing is in flight,
    // flush immediately instead of waiting for idle. Keeps the first batch from being
    // delayed by 800 ms when the user is clearly mid-bulk-delete.
    if (pendingRef.current.size >= BATCH_SIZE_THRESHOLD && !inFlightRef.current) {
      if (flushTimerRef.current) {
        clearTimeout(flushTimerRef.current);
        flushTimerRef.current = null;
      }
      flush();
      return;
    }

    // Idle trigger: user stopped clicking — send whatever is queued after FLUSH_DELAY_MS.
    if (flushTimerRef.current) clearTimeout(flushTimerRef.current);
    flushTimerRef.current = setTimeout(() => { flush(); }, FLUSH_DELAY_MS);
  }, [flush]);

  // INVARIANT(data-loss): pending deletes must survive page navigation. sendBeacon delivers the
  // POST body even after the page is unloaded; without this, the user's bulk-delete
  // would be silently dropped on refresh and ghost refs would reappear in the list.  Why: sendBeacon delivers the delete POST after unload; without it a bulk-delete on refresh would be dropped and deleted refs would reappear as ghosts.
  // ARCH: split unload vs unmount. Page unload uses sendBeacon (only transport that
  // works after the document is torn down). Component unmount on a live page (e.g.
  // document switch) uses a normal POST — debuggable and avoids double-sends when
  // both unmount and pagehide fire on tab close.
  useEffect(() => {
    const onPageUnload = () => {
      if (pendingRef.current.size === 0) return;
      const ids = Array.from(pendingRef.current.keys());
      const blob = new Blob([JSON.stringify({ reference_ids: ids })], { type: 'application/json' });
      navigator.sendBeacon(`/api${BATCH_ENDPOINT}`, blob);
      pendingRef.current.clear();
    };
    window.addEventListener('beforeunload', onPageUnload);
    window.addEventListener('pagehide', onPageUnload);
    return () => {
      window.removeEventListener('beforeunload', onPageUnload);
      window.removeEventListener('pagehide', onPageUnload);
      if (flushTimerRef.current) {
        clearTimeout(flushTimerRef.current);
        flushTimerRef.current = null;
      }
      if (pendingRef.current.size > 0) {
        const ids = Array.from(pendingRef.current.keys());
        pendingRef.current.clear();
        // Fire-and-forget: hook is being torn down; backend's deleted_at filter keeps
        // state consistent if this POST fails — the next list/fetch won't return them.
        apiClient.post(BATCH_ENDPOINT, { reference_ids: ids }).catch(() => {});
      }
    };
  }, []);

  return { scheduleDelete, flush };
}

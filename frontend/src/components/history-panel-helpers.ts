/** Pure helpers for the checkpoint history (Info) panel.
 *
 * Extracted from HistoryPanel so the upsert-by-id and dedup logic are unit-testable
 * without rendering the full component (API, hooks). */
// SYSTEM: checkpoint-history — pure timeline helpers

import type { Checkpoint } from '../types';

/** Upsert a snapshot into the list by checkpoint_id: replace an existing same-id row
 * in place, else prepend. A last-session upsert reuses the same checkpoint_id, so a
 * plain "dedup by id and drop" would silently ignore the update — this replaces it. */
export function upsertSnapshotById(prev: Checkpoint[], snap: Checkpoint): Checkpoint[] {
  const idx = prev.findIndex(s => s.checkpoint_id === snap.checkpoint_id);
  if (idx === -1) return [snap, ...prev];
  const next = prev.slice();
  next[idx] = snap;
  return next;
}

/** Append a server-fetched snapshot batch, deduping by checkpoint_id.
 *
 * The backend returns newest-first; upsert events (snapshot-created) may already
 * have inserted rows that appear in a later top-up batch, so a naive concat would
 * duplicate them. Rows already present (by id) are dropped from the appended
 * batch. Preserves the existing order and appends the fresh tail. */
export function appendDedupSnapshots(existing: Checkpoint[], batch: Checkpoint[]): Checkpoint[] {
  const seen = new Set(existing.map(s => s.checkpoint_id));
  const fresh = batch.filter(s => !seen.has(s.checkpoint_id));
  return fresh.length ? [...existing, ...fresh] : existing;
}

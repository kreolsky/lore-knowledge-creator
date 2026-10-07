/**
 * Inbox API — see SYSTEM: inbox (backend/inbox.py).
 * One pool per document: externally arrived notes/references carry an unread
 * flag for the key owner. These thin wrappers are the frontend's only touch
 * points: the pool summary (tab/tree tint), the open-clears call, and the
 * per-(user × document) notify toggles (Access tab).
 */
import { apiClient } from './client';

export type InboxKind = 'note' | 'ref';

export interface InboxDocCounts {
  notes: number;
  refs: number;
}

/** POST /api/inbox/read — clear the caller's flag on ONE object. 204 either
 * way (a non-recipient gets the same 204 as a no-op), so no error path here. */
export function readInboxObject(kind: InboxKind, id: string): Promise<void> {
  return apiClient.post('/inbox/read', { kind, id });
}

/** GET /api/projects/{id}/inbox — {documents: {docId: {notes, refs}}}. */
export async function fetchInboxSummary(projectId: string): Promise<Record<string, InboxDocCounts>> {
  // apiClient is untyped at the boundary (SYSTEM: api-client) — cast here.
  const data = await apiClient.get(`/projects/${projectId}/inbox`) as
    { documents?: Record<string, InboxDocCounts> } | undefined;
  return data?.documents ?? {};
}

export interface InboxToggles {
  notes: boolean;
  refs: boolean;
}

export function fetchInboxToggles(projectId: string, documentId: string): Promise<InboxToggles> {
  return apiClient.get(`/projects/${projectId}/inbox/toggles/${documentId}`) as Promise<InboxToggles>;
}

/** PUT — partial upsert; omitted fields keep their value. Returns the full pair. */
export function setInboxToggles(
  projectId: string,
  documentId: string,
  patch: Partial<InboxToggles>,
): Promise<InboxToggles> {
  return apiClient.put(`/projects/${projectId}/inbox/toggles/${documentId}`, patch) as Promise<InboxToggles>;
}

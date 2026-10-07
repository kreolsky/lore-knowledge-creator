/**
 * Inbox store — the caller's unread pool, per project.
 *
 * see SYSTEM: inbox — a note or reference that arrived from OUTSIDE (a widget
 * key, or an MCP key over the gateway) carries an unread flag for the key
 * owner. This store mirrors the backend summary (GET /api/projects/{id}/inbox)
 * and lives for the TINT surfaces: the notes/refs right-tab buttons and
 * the document tree. It is EPHEMERAL server state — deliberately NOT in
 * ui-store's persisted prefs blob: the summary is per-user server truth, not a
 * layout preference, and it refetches on ws:inbox_changed anyway.
 */
// ARCH: read-through = the ONLY decrement. Opening a flagged object
// decrements the matching count locally FIRST, then calls readInboxObject
// (POST /api/inbox/read); ws:inbox_changed fires for THIS user on every
// change (the backend handler is owner-filtered) and refetches — the local
// decrement is just the no-latency paint, the refetch is the truth.

import { useEffect } from 'react';
import { create } from 'zustand';
import { off, on } from '../events';
import { t } from '../i18n';
import { useAppStore } from './app-store';
import {
  fetchInboxSummary, readInboxObject, setInboxToggles,
  type InboxDocCounts, type InboxKind, type InboxToggles,
} from '../api/inbox';

interface InboxState {
  /** Per-document unread counts for the CURRENT project (absent = none). */
  summary: Record<string, InboxDocCounts>;
  /** Resolved toggles per (projectId:docId) — the Access tab surface. */
  toggles: Record<string, InboxToggles>;
  loadSummary: (projectId: string | null | undefined) => Promise<void>;
  clearSummary: () => void;
  /** Local optimistic decrement; returns whether a count was decremented. */
  applyRead: (documentId: string | null | undefined, kind: InboxKind) => boolean;
  /** Rollback of applyRead when the read POST failed. */
  undoRead: (documentId: string, kind: InboxKind) => void;
  /** Persist a toggle flip and store the resolved pair. */
  updateToggles: (
    projectId: string, documentId: string, patch: Partial<InboxToggles>,
  ) => Promise<void>;
  /** Seed toggles from a GET (no write). */
  seedToggles: (projectId: string, documentId: string, value: InboxToggles) => void;
  getToggles: (projectId: string, documentId: string) => InboxToggles | null;
}

export const useInboxStore = create<InboxState>((set, get) => ({
  summary: {},
  toggles: {},

  loadSummary: async (projectId) => {
    if (!projectId) { set({ summary: {} }); return; }
    let summary: Record<string, InboxDocCounts>;
    try {
      summary = await fetchInboxSummary(projectId);
    } catch (err) {
      // No silent degradation: an unpainted tree reads as "nothing unread",
      // so a failed load must be told apart from an empty pool.
      console.error('Failed to load inbox summary', err);
      useAppStore.getState().showToast(t('inboxSummaryLoadFailed'), 'error');
      return;
    }
    releaseAutoOpenOnArrival(get().summary, summary);
    set({ summary });
  },

  clearSummary: () => set({ summary: {} }),

  applyRead: (documentId, kind) => {
    // The summary counts keys are plural ('notes'/'refs'); the read call's kind
    // is singular ('note'/'ref') — one mapping, here, at the seam.
    if (!documentId) return false;
    const key = countKey(kind);
    const cur = get().summary[documentId];
    if (!cur || cur[key] <= 0) return false;
    set({ summary: { ...get().summary, [documentId]: { ...cur, [key]: cur[key] - 1 } } });
    return true;
  },

  undoRead: (documentId, kind) => {
    const key = countKey(kind);
    const cur = get().summary[documentId] ?? { notes: 0, refs: 0 };
    set({ summary: { ...get().summary, [documentId]: { ...cur, [key]: cur[key] + 1 } } });
  },

  updateToggles: async (projectId, documentId, patch) => {
    const resolved = await setInboxToggles(projectId, documentId, patch);
    set({ toggles: { ...get().toggles, [`${projectId}:${documentId}`]: resolved } });
  },

  seedToggles: (projectId, documentId, value) => {
    set({ toggles: { ...get().toggles, [`${projectId}:${documentId}`]: value } });
  },

  getToggles: (projectId, documentId) => get().toggles[`${projectId}:${documentId}`] ?? null,
}));

// The summary counts keys are plural ('notes'/'refs'); the read call's kind
// is singular ('note'/'ref') — one mapping, here, at the seam.
function countKey(kind: InboxKind): keyof InboxDocCounts {
  return kind === 'note' ? 'notes' : 'refs';
}

/**
 * Opens ONE flagged object: decrements locally, then fires the read call.
 * The single helper both open paths (notes tab, refs tab) share — read on open.
 */
export async function openInboxObject(
  documentId: string | null | undefined,
  kind: InboxKind,
  id: string,
): Promise<void> {
  // WHY: decrement BEFORE the POST — the owner-filtered ws:inbox_changed refetch
  // can land before the POST resolves, and a decrement after it would subtract
  // the same read twice (a count of 0 while one arrival is still unread).
  const decremented = useInboxStore.getState().applyRead(documentId, kind);
  try {
    await readInboxObject(kind, id);
  } catch (err) {
    console.error('Failed to mark inbox object read', err);
    if (decremented && documentId) useInboxStore.getState().undoRead(documentId, kind);
    useAppStore.getState().showToast(t('inboxReadFailed'), 'error');
  }
}

// INVARIANT: the earliest-unread auto-open fires once per (kind × document)
// until a NEW arrival raises that document's count; a read never re-arms it.
// Why: the panels unmount on every tab switch, so an in-component guard would
// drag the user back into a thread they just closed, while a session-long
// guard would never surface a note that arrived after the first auto-open.
const autoOpened = new Set<string>();

/** Claims the auto-open for (kind, document); false when already spent. */
export function claimAutoOpen(kind: InboxKind, documentId: string): boolean {
  const key = `${kind}:${documentId}`;
  if (autoOpened.has(key)) return false;
  autoOpened.add(key);
  return true;
}

function releaseAutoOpenOnArrival(
  prev: Record<string, InboxDocCounts>,
  next: Record<string, InboxDocCounts>,
): void {
  for (const [docId, counts] of Object.entries(next)) {
    const before = prev[docId] ?? { notes: 0, refs: 0 };
    if (counts.notes > before.notes) autoOpened.delete(`note:${docId}`);
    if (counts.refs > before.refs) autoOpened.delete(`ref:${docId}`);
  }
}

let _subscriptionMounted = false;

/**
 * Project-level wiring, mounted ONCE per project page (ProjectPage):
 * loads the summary, refetches on the owner-filtered ws:inbox_changed frame.
 * Returns the current document's counts (absent = none).
 */
export function useInboxSummary(
  projectId: string | null | undefined,
  currentDocId: string | null | undefined,
): InboxDocCounts {
  const summary = useInboxStore(s => s.summary);
  const loadSummary = useInboxStore(s => s.loadSummary);

  useEffect(() => {
    void loadSummary(projectId);
  }, [projectId, loadSummary]);

  useEffect(() => {
    if (!projectId || _subscriptionMounted) return;
    _subscriptionMounted = true;
    const handler = () => { void loadSummary(projectId); };
    on('ws:inbox_changed', handler);
    return () => {
      _subscriptionMounted = false;
      off('ws:inbox_changed', handler);
    };
  }, [projectId, loadSummary]);

  return (currentDocId ? summary[currentDocId] : undefined) ?? { notes: 0, refs: 0 };
}

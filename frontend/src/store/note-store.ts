/** Zustand store for note state — notes collections, thread tracking, pending navigation. */
// ARCH: Isolated slice from app-store — note data has no persistence side effects.
// ARCH: app-store reads this store for setCurrentDocument thread resolution.
// SYSTEM: note-store — note collections, active thread, pending navigation

import { create } from 'zustand';
import type { RightTab } from './ui-store';

interface NoteState {
  activeNoteThreadId: string | null;
  documentNoteThreads: Record<string, string | null>;
  pendingNoteNavigation: { noteId: string; parentNoteId?: string | null } | null;
  connectedNoteId: string | null;
  previousRightTab: RightTab | null;

  setActiveNoteThreadId: (id: string | null, docId?: string | null) => void;
  /** Forget a deleted thread everywhere: the open thread and every per-doc memory of it. */
  clearNoteThread: (id: string) => void;
  setPendingNoteNavigation: (nav: { noteId: string; parentNoteId?: string | null } | null) => void;
  setConnectedNoteId: (id: string | null) => void;
  setPreviousRightTab: (tab: RightTab | null) => void;
  resetForProjectSwitch: () => void;
}

export const useNoteStore = create<NoteState>((set) => ({
  // INVARIANT: maps visited doc IDs to active thread IDs; cleared on doc switch  Why: per-doc active thread memory; cleared on doc switch so a stale thread from another doc never leaks in.
  activeNoteThreadId: null,
  documentNoteThreads: {},
  // WHY: transient produce-consume lifecycle; consumed once then cleared  Why: pendingNoteNavigation is a one-shot signal (produce on note open, consume on render, then clear) so it doesn't re-fire on re-render.
  pendingNoteNavigation: null,
  connectedNoteId: null,
  previousRightTab: null,

  setActiveNoteThreadId: (id, docId) => set((prev) => ({
    activeNoteThreadId: id,
    documentNoteThreads: docId ? { ...prev.documentNoteThreads, [docId]: id } : prev.documentNoteThreads,
  })),
  clearNoteThread: (id) => set((prev) => ({
    activeNoteThreadId: prev.activeNoteThreadId === id ? null : prev.activeNoteThreadId,
    documentNoteThreads: Object.fromEntries(
      Object.entries(prev.documentNoteThreads).map(([doc, tid]) => [doc, tid === id ? null : tid]),
    ),
  })),
  setPendingNoteNavigation: (nav) => set({ pendingNoteNavigation: nav }),
  setConnectedNoteId: (id) => set({ connectedNoteId: id }),
  setPreviousRightTab: (tab) => set({ previousRightTab: tab }),
  // WHY all five fields: a project switch must not carry note state across
  // projects — the per-doc remembered thread is intentionally forgotten
  // (document ids are project-unique, so the map is pure stale memory after a
  // switch), and a pending one-shot navigation or connector note left over
  // would fire against the new project's documents.
  resetForProjectSwitch: () => set({
    activeNoteThreadId: null,
    documentNoteThreads: {},
    pendingNoteNavigation: null,
    connectedNoteId: null,
    previousRightTab: null,
  }),
}));

/** resetForProjectSwitch resets ALL five fields — the per-doc thread memory,
 * the one-shot pending navigation and the connector's note must not survive a
 * project switch (document ids are project-unique uuids, so keeping them is
 * stale memory, and the remembered thread is intentionally forgotten when the
 * project changes). */
// @vitest-environment jsdom

import { describe, it, expect, beforeEach } from 'vitest';
import { useNoteStore } from './note-store';

beforeEach(() => {
  useNoteStore.setState({
    activeNoteThreadId: 't1',
    documentNoteThreads: { d1: 't1', d2: 't2' },
    pendingNoteNavigation: { noteId: 't2' },
    connectedNoteId: 't1',
    previousRightTab: 'chat',
  });
});

describe('note-store resetForProjectSwitch — five-field reset', () => {
  it('resets activeNoteThreadId and previousRightTab (the original two)', () => {
    useNoteStore.getState().resetForProjectSwitch();
    const s = useNoteStore.getState();
    expect(s.activeNoteThreadId).toBeNull();
    expect(s.previousRightTab).toBeNull();
  });

  it('resets documentNoteThreads — the per-doc remembered thread does not cross projects', () => {
    useNoteStore.getState().resetForProjectSwitch();
    expect(useNoteStore.getState().documentNoteThreads).toEqual({});
  });

  it('resets pendingNoteNavigation and connectedNoteId', () => {
    useNoteStore.getState().resetForProjectSwitch();
    const s = useNoteStore.getState();
    expect(s.pendingNoteNavigation).toBeNull();
    expect(s.connectedNoteId).toBeNull();
  });
});

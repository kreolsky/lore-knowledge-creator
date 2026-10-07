/**
 * Tests for inbox-store — the read-on-open decrement, its rollback, and the
 * earliest-unread auto-open guard (see SYSTEM: inbox).
 */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach } from 'vitest';

const showToast = vi.fn();
vi.mock('./app-store', () => ({
  useAppStore: { getState: () => ({ showToast }) },
}));
vi.mock('../i18n', () => ({ t: (k: string) => k }));
vi.mock('../api/inbox', () => ({
  fetchInboxSummary: vi.fn(),
  readInboxObject: vi.fn(),
  setInboxToggles: vi.fn(),
}));

import { fetchInboxSummary, readInboxObject } from '../api/inbox';
import { claimAutoOpen, openInboxObject, useInboxStore } from './inbox-store';

const fetchMock = fetchInboxSummary as ReturnType<typeof vi.fn>;
const readMock = readInboxObject as ReturnType<typeof vi.fn>;

beforeEach(() => {
  showToast.mockReset();
  fetchMock.mockReset();
  readMock.mockReset();
  useInboxStore.setState({ summary: {}, toggles: {} });
});

describe('openInboxObject', () => {
  it('a refetch landing before the read POST resolves does not subtract the read twice', async () => {
    useInboxStore.setState({ summary: { d1: { notes: 2, refs: 0 } } });
    let resolvePost!: () => void;
    readMock.mockReturnValue(new Promise<void>((r) => { resolvePost = r; }));
    const pending = openInboxObject('d1', 'note', 'n1');
    // The owner's ws:inbox_changed refetch arrives first, already without n1.
    fetchMock.mockResolvedValue({ d1: { notes: 1, refs: 0 } });
    await useInboxStore.getState().loadSummary('p1');
    resolvePost();
    await pending;
    expect(useInboxStore.getState().summary.d1.notes).toBe(1);
  });

  it('a failed read rolls the count back and tells the user', async () => {
    useInboxStore.setState({ summary: { d1: { notes: 1, refs: 0 } } });
    readMock.mockRejectedValue(new Error('500'));
    await openInboxObject('d1', 'note', 'n1');
    expect(useInboxStore.getState().summary.d1.notes).toBe(1);
    expect(showToast).toHaveBeenCalledWith('inboxReadFailed', 'error');
  });
});

describe('loadSummary', () => {
  it('a failed load keeps the previous pool and shows an error, not an empty tree', async () => {
    useInboxStore.setState({ summary: { d1: { notes: 1, refs: 0 } } });
    fetchMock.mockRejectedValue(new Error('500'));
    await useInboxStore.getState().loadSummary('p1');
    expect(useInboxStore.getState().summary.d1.notes).toBe(1);
    expect(showToast).toHaveBeenCalledWith('inboxSummaryLoadFailed', 'error');
  });
});

describe('claimAutoOpen', () => {
  it('is spent once per document and re-armed only by a NEW arrival, never by a read', async () => {
    expect(claimAutoOpen('note', 'd-arm')).toBe(true);
    expect(claimAutoOpen('note', 'd-arm')).toBe(false);
    // A read lowers the count — still spent.
    useInboxStore.setState({ summary: { 'd-arm': { notes: 2, refs: 0 } } });
    fetchMock.mockResolvedValue({ 'd-arm': { notes: 1, refs: 0 } });
    await useInboxStore.getState().loadSummary('p1');
    expect(claimAutoOpen('note', 'd-arm')).toBe(false);
    // A refs arrival does not re-arm the notes guard.
    fetchMock.mockResolvedValue({ 'd-arm': { notes: 1, refs: 1 } });
    await useInboxStore.getState().loadSummary('p1');
    expect(claimAutoOpen('note', 'd-arm')).toBe(false);
    // A new note arrival re-arms it.
    fetchMock.mockResolvedValue({ 'd-arm': { notes: 2, refs: 1 } });
    await useInboxStore.getState().loadSummary('p1');
    expect(claimAutoOpen('note', 'd-arm')).toBe(true);
  });
});

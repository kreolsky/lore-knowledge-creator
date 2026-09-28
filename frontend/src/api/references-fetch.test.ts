/**
 * In-flight dedup for the panel /references fetch — same contract as the
 * chat-store `loadSessions` dedup: concurrent same-scope GETs collapse to one
 * round-trip; a different scope refetches; a rejected fetch evicts so a retry
 * refetches. See the refs-fetch-dedup-followup plan.
 *
 * ARCH (refs-list-no-content): index_doc_id is no longer part of the scope key or
 * the request — the server resolves the project index doc itself. The scope key is
 * (project, document, includeArchived) — the archive flag is folded in (plan
 * reference-archive-v2) so the SWR cache + in-flight dedup treat "Show archived" as
 * a distinct scope instead of serving the stale non-archived list.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('./client', () => ({
  apiClient: { get: vi.fn() },
}));

import { apiClient } from './client';
import { loadReferences, clearReferencesInFlight } from './references-fetch';

const mockGet = apiClient.get as ReturnType<typeof vi.fn>;

describe('loadReferences in-flight dedup', () => {
  beforeEach(() => {
    clearReferencesInFlight();
    mockGet.mockReset();
  });

  it('collapses two concurrent same-scope calls into ONE apiClient.get', async () => {
    let resolve!: (v: unknown) => void;
    mockGet.mockReturnValue(new Promise((r) => { resolve = r; }));

    const p1 = loadReferences('proj', 'docA');
    const p2 = loadReferences('proj', 'docA');

    expect(mockGet).toHaveBeenCalledTimes(1);

    resolve([{ reference_id: 'r1' }]);
    expect(await p1).toEqual([{ reference_id: 'r1' }]);
    expect(await p2).toEqual([{ reference_id: 'r1' }]);
  });

  it('issues a second get for a different documentId (distinct scope key)', async () => {
    mockGet.mockResolvedValue([]);

    await loadReferences('proj', 'docA');
    await loadReferences('proj', 'docB');

    expect(mockGet).toHaveBeenCalledTimes(2);
    expect(mockGet.mock.calls[0][0]).toContain('document_id=docA');
    expect(mockGet.mock.calls[1][0]).toContain('document_id=docB');
  });

  it('evicts a rejected entry so the next call refetches', async () => {
    mockGet.mockRejectedValueOnce(new Error('boom'));

    await expect(loadReferences('proj', 'docA')).rejects.toThrow('boom');

    mockGet.mockResolvedValueOnce([{ reference_id: 'r2' }]);
    expect(await loadReferences('proj', 'docA')).toEqual([{ reference_id: 'r2' }]);
    expect(mockGet).toHaveBeenCalledTimes(2);
  });

  it('SWR: paints cached refs synchronously on a re-open, then revalidates', async () => {
    mockGet.mockResolvedValueOnce([{ reference_id: 'r1' }]);
    // First open: no cache yet, onCached not fired; result populates the cache.
    const firstCached = vi.fn();
    await loadReferences('proj', 'docA', firstCached);
    expect(firstCached).not.toHaveBeenCalled();

    // Second open of the SAME scope: cached list paints synchronously (before await),
    // and the fetch still fires (revalidate).
    mockGet.mockResolvedValueOnce([{ reference_id: 'r1-fresh' }]);
    const onCached = vi.fn();
    const p = loadReferences('proj', 'docA', onCached);
    expect(onCached).toHaveBeenCalledTimes(1);
    expect(onCached).toHaveBeenCalledWith([{ reference_id: 'r1' }]);
    expect(mockGet).toHaveBeenCalledTimes(2);
    expect(await p).toEqual([{ reference_id: 'r1-fresh' }]);
  });

  it('SWR: never fires onCached for a scope with no cached entry', async () => {
    mockGet.mockResolvedValue([]);
    const onCached = vi.fn();
    await loadReferences('proj', 'docNew', onCached);
    expect(onCached).not.toHaveBeenCalled();
  });

  it('never includes index_doc_id in the query (server resolves it)', async () => {
    mockGet.mockResolvedValue([]);

    await loadReferences('proj', 'docA');

    expect(mockGet).toHaveBeenCalledWith('/references?document_id=docA');
  });

  it('appends include_archived=true when the flag is set', async () => {
    mockGet.mockResolvedValue([]);

    await loadReferences('proj', 'docA', undefined, true);

    expect(mockGet).toHaveBeenCalledWith(
      '/references?document_id=docA&include_archived=true',
    );
  });

  it('includeArchived is a DISTINCT scope (not served the non-archived SWR cache)', async () => {
    // Populate the non-archived scope.
    mockGet.mockResolvedValueOnce([{ reference_id: 'r-nonarch' }]);
    await loadReferences('proj', 'docA');

    // A cached paint for the NON-archived scope must NOT fire for the archived scope.
    mockGet.mockResolvedValueOnce([{ reference_id: 'r-arch' }]);
    const onCached = vi.fn();
    const p = loadReferences('proj', 'docA', onCached, true);
    expect(onCached).not.toHaveBeenCalled();
    expect(mockGet).toHaveBeenCalledTimes(2);
    expect(await p).toEqual([{ reference_id: 'r-arch' }]);
  });

  it('includeArchived dedup is per-scope: concurrent archived + non-archived issue 2 gets', async () => {
    let resolveA!: (v: unknown) => void;
    let resolveN!: (v: unknown) => void;
    mockGet.mockImplementationOnce(() => new Promise((r) => { resolveA = r; }));
    mockGet.mockImplementationOnce(() => new Promise((r) => { resolveN = r; }));

    const pArch = loadReferences('proj', 'docA', undefined, true);
    const pNon = loadReferences('proj', 'docA');

    // Two distinct scopes → two round-trips (not one collapsed call).
    expect(mockGet).toHaveBeenCalledTimes(2);

    resolveA([{ reference_id: 'ra' }]);
    resolveN([{ reference_id: 'rn' }]);
    expect((await pArch)[0].reference_id).toBe('ra');
    expect((await pNon)[0].reference_id).toBe('rn');
  });
});

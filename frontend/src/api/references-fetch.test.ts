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

  it('evicts a rejected entry so the next call refetches', async () => {
    mockGet.mockRejectedValueOnce(new Error('boom'));

    await expect(loadReferences('proj', 'docA')).rejects.toThrow('boom');

    mockGet.mockResolvedValueOnce([{ reference_id: 'r2' }]);
    expect(await loadReferences('proj', 'docA')).toEqual([{ reference_id: 'r2' }]);
    expect(mockGet).toHaveBeenCalledTimes(2);
  });
});

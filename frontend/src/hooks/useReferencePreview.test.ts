/**
 * Behaviour matrix for fetchRefPreview on the resource-cache primitive: in-flight
 * dedup (same contract as loadReferences), failure contract, logout + project-switch
 * lifecycle, seed-without-GET.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('../api/client', () => ({
  apiClient: { get: vi.fn() },
}));

import { apiClient } from '../api/client';
import { fetchRefPreview, clearRefPreviewCache } from './useReferencePreview';

const mockGet = apiClient.get as ReturnType<typeof vi.fn>;

describe('fetchRefPreview failure is an error, never an empty body', () => {
  beforeEach(() => {
    clearRefPreviewCache();
    mockGet.mockReset();
  });

  it('REJECTS when apiClient.get rejects, instead of resolving {content: ""}', async () => {
    mockGet.mockRejectedValue(new Error('network'));
    await expect(fetchRefPreview('r5')).rejects.toThrow('network');
  });

  it('does not cache the failure — a second call re-fetches', async () => {
    mockGet.mockRejectedValueOnce(new Error('network'));
    await expect(fetchRefPreview('r6')).rejects.toThrow('network');

    mockGet.mockResolvedValueOnce({ reference_id: 'r6', media_type: 'markdown', content: 'recovered' });
    await expect(fetchRefPreview('r6')).resolves.toEqual({ content: 'recovered' });
    expect(mockGet).toHaveBeenCalledTimes(2);
  });

  it('a genuinely-empty ref still resolves "" and is cached (not retried)', async () => {
    mockGet.mockResolvedValue({ reference_id: 'r7', media_type: 'markdown', content: '' });
    await expect(fetchRefPreview('r7')).resolves.toEqual({ content: '' });
    await expect(fetchRefPreview('r7')).resolves.toEqual({ content: '' });
    expect(mockGet).toHaveBeenCalledTimes(1);
  });
});

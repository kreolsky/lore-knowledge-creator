/**
 * Tests for chat-message-crud — the ONE shared request layer for message
 * load/edit/delete against /chat/sessions/{id}/messages and /chat/messages/{id},
 * consumed by BOTH the AI chat-store and the note-chat store (scope
 * discrimination lives in the callers' callbacks). Pins the shared liveness
 * guard and error surfacing so the two stores cannot drift.
 */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach } from 'vitest';

const showToast = vi.fn();
vi.mock('./app-store', () => ({
  useAppStore: { getState: () => ({ showToast }) },
}));
vi.mock('../i18n', () => ({ t: (k: string) => k }));
vi.mock('../api/client', () => ({
  apiClient: {
    get: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}));

import { apiClient } from '../api/client';
import {
  loadMessagesFor,
  patchMessageContent,
  deleteMessageById,
  type MessageLoadScope,
} from './chat-message-crud';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('loadMessagesFor', () => {
  const MESSAGES = [{ message_id: 'm1', content: 'hello' }];

  function makeScope(activeId: string | null) {
    const scope: MessageLoadScope = {
      activeSessionId: () => activeId,
      onLoaded: vi.fn(),
      onLoadError: vi.fn(),
    };
    return scope;
  }

  it('commits messages to a live scope', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(MESSAGES as never);
    const scope = makeScope('s1');
    await loadMessagesFor('s1', scope);
    expect(scope.onLoaded).toHaveBeenCalledWith(MESSAGES);
    expect(scope.onLoadError).not.toHaveBeenCalled();
    expect(showToast).not.toHaveBeenCalled();
  });

  it('staleness guard: a superseded fetch never commits', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(MESSAGES as never);
    const scope = makeScope('other-session'); // user switched while in flight
    await loadMessagesFor('s1', scope);
    expect(scope.onLoaded).not.toHaveBeenCalled();
    expect(scope.onLoadError).not.toHaveBeenCalled();
  });

  it('failure surfaces a toast and the error terminal only to the live scope', async () => {
    vi.mocked(apiClient.get).mockRejectedValueOnce(new Error('boom'));
    const live = makeScope('s1');
    await loadMessagesFor('s1', live);
    expect(live.onLoadError).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith('failedToLoadMessages', 'error');

    const stale = makeScope('s2');
    await loadMessagesFor('s1', stale);
    expect(stale.onLoadError).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith('failedToLoadMessages', 'error'); // toast is unconditional (both stores toast on stale failure)
  });
});

describe('patchMessageContent', () => {
  it('returns the updated message on success', async () => {
    const updated = { message_id: 'm1', content: 'EDITED' };
    vi.mocked(apiClient.patch).mockResolvedValueOnce(updated as never);
    await expect(patchMessageContent('m1', 'EDITED')).resolves.toEqual(updated);
    expect(showToast).not.toHaveBeenCalled();
  });

  it('returns null and surfaces the toast on failure', async () => {
    vi.mocked(apiClient.patch).mockRejectedValueOnce(new Error('boom'));
    await expect(patchMessageContent('m1', 'EDITED')).resolves.toBeNull();
    expect(showToast).toHaveBeenCalledWith('failedToEditMessage', 'error');
  });
});

describe('deleteMessageById', () => {
  it('returns true on success', async () => {
    vi.mocked(apiClient.delete).mockResolvedValueOnce(undefined as never);
    await expect(deleteMessageById('m1')).resolves.toBe(true);
    expect(showToast).not.toHaveBeenCalled();
  });

  it('returns false and surfaces the toast on failure', async () => {
    vi.mocked(apiClient.delete).mockRejectedValueOnce(new Error('boom'));
    await expect(deleteMessageById('m1')).resolves.toBe(false);
    expect(showToast).toHaveBeenCalledWith('failedToDeleteMessage', 'error');
  });
});

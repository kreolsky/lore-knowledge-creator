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

  it('staleness guard covers the raw rows too: a superseded fetch never reaches onRows', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce({ messages: MESSAGES, queue: [], queue_seq: null } as never);
    const scope = { ...makeScope('other-session'), onRows: vi.fn() };
    await loadMessagesFor('s1', scope);
    expect(scope.onRows).not.toHaveBeenCalled();
  });

  it('a live fetch hands the raw rows to onRows before onLoaded', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce({ messages: MESSAGES, queue: [], queue_seq: null } as never);
    const order: string[] = [];
    const scope: MessageLoadScope = {
      activeSessionId: () => 's1',
      onRows: vi.fn(() => { order.push('rows'); }),
      onLoaded: vi.fn(() => { order.push('loaded'); }),
      onLoadError: vi.fn(),
    };
    await loadMessagesFor('s1', scope);
    expect(order).toEqual(['rows', 'loaded']);
  });
});

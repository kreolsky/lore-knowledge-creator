/** useLazyMessageImages — lazy hydration of a message's base64 images.
 *
 * Minimal renderHook via createElement + createRoot (no @testing-library/react).
 */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import type { ChatMessage } from '../../types';

vi.mock('../../api/client', () => ({
  apiClient: { get: vi.fn() },
}));
import { apiClient } from '../../api/client';
import { useLazyMessageImages } from './useLazyMessageImages';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const mockGet = apiClient.get as ReturnType<typeof vi.fn>;

function msg(over: Partial<ChatMessage>): ChatMessage {
  return {
    message_id: 'm1', chat_id: 's1', parent_id: null, role: 'user',
    content: 'hi', created_at: '2026-01-01', ...over,
  };
}

let container: HTMLDivElement;
let root: Root;
let captured: ReturnType<typeof useLazyMessageImages>;

function render(message: ChatMessage) {
  function Test() {
    captured = useLazyMessageImages(message);
    return null;
  }
  act(() => root.render(createElement(Test)));
}

const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

beforeEach(() => {
  mockGet.mockReset();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe('useLazyMessageImages', () => {
  it('renders persisted images without fetching', () => {
    render(msg({ images: ['data:a'], image_count: 1 }));
    expect(captured.images).toEqual(['data:a']);
    expect(captured.count).toBe(1);
    expect(mockGet).not.toHaveBeenCalled();
  });

  it('does nothing when image_count is 0', () => {
    render(msg({ image_count: 0 }));
    expect(captured.count).toBe(0);
    expect(mockGet).not.toHaveBeenCalled();
  });

  it('lazy-fetches on mount when image_count>0 and images absent', async () => {
    mockGet.mockResolvedValueOnce({ images: ['data:x', 'data:y'] });
    render(msg({ image_count: 2 }));
    expect(mockGet).toHaveBeenCalledWith('/chat/sessions/s1/messages/m1/images');
    await flush();
    expect(captured.images).toEqual(['data:x', 'data:y']);
    expect(captured.error).toBe(false);
  });

  it('sets error on fetch failure (no silent degradation)', async () => {
    mockGet.mockRejectedValueOnce(new Error('boom'));
    render(msg({ image_count: 1 }));
    await flush();
    expect(captured.error).toBe(true);
    expect(captured.images).toBeUndefined();
  });

  it('ensureImages returns persisted array without a fetch', async () => {
    render(msg({ images: ['data:a'], image_count: 1 }));
    await act(async () => {
      expect(await captured.ensureImages()).toEqual(['data:a']);
    });
    expect(mockGet).not.toHaveBeenCalled();
  });
});

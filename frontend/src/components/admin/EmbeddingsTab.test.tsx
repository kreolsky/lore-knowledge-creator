/** EmbeddingsTab — the chat-recall index block and its "Index chats now" button.
 *
 * Pins the frontend contract:
 * - indexing off renders the off line (never a 0 for pending) and a disabled button;
 * - indexing on with work renders the numbers and an enabled button;
 * - a failed POST shows the error toast — no silent catch.
 *
 * Harness: manual createRoot + act (mirrors InfoTab.test.tsx); the api client is
 * mocked at the module boundary.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let EmbeddingsTab: typeof import('./EmbeddingsTab').EmbeddingsTab;
let container: HTMLDivElement;
let root: Root;
let showToast: ReturnType<typeof vi.fn>;
let apiGet: ReturnType<typeof vi.fn>;
let apiPost: ReturnType<typeof vi.fn>;

const DOC_STATS = {
  doc_chunks_count: 5,
  docs_without_embeddings: 0,
  refs_without_embeddings: 0,
  pending_reembed_count: 0,
  stale_model_count: 0,
};
const STATUS = { running: false, operation: 'embedding', processed: 0, total: 0, errors: 0 };

const tFn = (k: string, vars?: Record<string, string | number>) =>
  Object.entries(vars ?? {}).reduce((s, [v, x]) => s.replace(`{${v}}`, String(x)), k);

function mockStats(chat: Record<string, unknown>) {
  apiGet.mockImplementation((url: string) =>
    Promise.resolve(url === '/admin/embeddings/stats' ? { ...DOC_STATS, chat } : STATUS));
}

beforeEach(async () => {
  vi.resetModules();
  showToast = vi.fn();
  apiGet = vi.fn();
  apiPost = vi.fn(() => Promise.resolve({}));

  vi.doMock('../../api/client', async () => {
    const actual = await vi.importActual<Record<string, unknown>>('../../api/client');
    return {
      ...actual,
      apiClient: {
        get: apiGet,
        post: apiPost,
        patch: vi.fn(() => Promise.resolve({})),
        put: vi.fn(() => Promise.resolve({})),
        delete: vi.fn(() => Promise.resolve({})),
      },
    };
  });
  vi.doMock('../../store/app-store', () => {
    const appState = { showToast };
    const useAppStore = (sel: (s: Record<string, unknown>) => unknown) => sel(appState);
    (useAppStore as unknown as { getState: () => Record<string, unknown> }).getState = () => appState;
    return { useAppStore };
  });
  vi.doMock('../../i18n', () => ({
    t: tFn,
    useTranslation: () => ({ t: tFn }),
  }));

  ({ EmbeddingsTab } = await import('./EmbeddingsTab'));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../../api/client');
  vi.doUnmock('../../store/app-store');
  vi.doUnmock('../../i18n');
});

async function render() {
  await act(async () => { root.render(createElement(EmbeddingsTab)); });
  await act(async () => {});
}

function indexChatsButton(): HTMLButtonElement {
  const btn = Array.from(container.querySelectorAll('button')).find(b => b.textContent?.trim() === 'indexChatsNow');
  expect(btn).toBeDefined();
  return btn as HTMLButtonElement;
}

describe('EmbeddingsTab — chat index', () => {
  it('indexing off renders the off line, no pending number, and a disabled button', async () => {
    mockStats({ state: 'skill_disabled', chunks: 3, pending_messages: null, stale_chunks: null });
    await render();
    expect(container.querySelector('[data-chat-index-off]')?.textContent).toBe('chatIndexOffSkill');
    expect(container.textContent).not.toContain('chatPendingMessages');
    expect(indexChatsButton().disabled).toBe(true);
  });

  it('indexing on with work renders the numbers and an enabled button', async () => {
    mockStats({ state: 'on', chunks: 3, pending_messages: 2, stale_chunks: 0 });
    await render();
    expect(container.textContent).toContain('chatPendingMessages: 2');
    expect(container.querySelector('[data-chat-index-off]')).toBeNull();
    expect(indexChatsButton().disabled).toBe(false);
  });

  it('a failed POST shows the error toast', async () => {
    mockStats({ state: 'on', chunks: 0, pending_messages: 1, stale_chunks: 0 });
    apiPost.mockImplementation(() => Promise.reject(new Error('500')));
    await render();
    await act(async () => { indexChatsButton().click(); });
    await act(async () => {});
    expect(apiPost).toHaveBeenCalledWith('/admin/embeddings/index-chats', {});
    expect(showToast).toHaveBeenCalledWith('failedToStartChatIndex', 'error');
  });
});

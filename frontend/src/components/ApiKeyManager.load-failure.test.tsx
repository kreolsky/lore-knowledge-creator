/**
 * ApiKeyManager — a failed key-list load says so instead of looking like
 * "no keys yet" (empty state must be distinct from error state).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, forwardRef, act, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let ApiKeyManager: typeof import('./ApiKeyManager').default;
let showToast: ReturnType<typeof vi.fn>;

beforeEach(async () => {
  vi.resetModules();
  showToast = vi.fn();
  const appState = { showToast, documents: [], setDocuments: vi.fn(), currentProject: null };
  vi.doMock('../api/client', () => ({
    apiClient: {
      get: vi.fn(() => Promise.reject(new Error('boom'))),
      post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
    },
    HttpError: class HttpError extends Error {},
  }));
  vi.doMock('./ui', () => ({
    Button: (p: { onClick?: () => void; children?: ReactNode }) => createElement('button', { type: 'button', onClick: p.onClick }, p.children),
    IconButton: (p: { title?: string; children?: ReactNode }) => createElement('button', { type: 'button', title: p.title }, p.children),
    FieldCheckbox: () => null,
    FieldInput: forwardRef<HTMLInputElement, Record<string, unknown>>((props, ref) => createElement('input', { ...props, ref } as never)),
  }));
  vi.doMock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
  vi.doMock('../store/app-store', () => ({
    useAppStore: Object.assign(
      (selector: (s: typeof appState) => unknown) => selector(appState),
      { getState: () => appState },
    ),
  }));
  ({ default: ApiKeyManager } = await import('./ApiKeyManager'));
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  for (const m of ['../api/client', './ui', '../i18n', '../store/app-store']) vi.doUnmock(m);
});

describe('ApiKeyManager key-list load failure', () => {
  it('toasts failedToLoadApiKeys', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {});
    await act(async () => { root.render(createElement(ApiKeyManager, { documentId: 'd1' })); });
    expect(showToast).toHaveBeenCalledWith('failedToLoadApiKeys', 'error');
    err.mockRestore();
  });
});

/**
 * Dashboard — a failed project-list load says so instead of looking like
 * "no projects yet" (empty state must be distinct from error state).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, forwardRef, act, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let Dashboard: typeof import('./Dashboard').Dashboard;
let appState: Record<string, unknown>;

beforeEach(async () => {
  vi.resetModules();
  appState = {
    projects: [],
    setProjects: vi.fn(),
    setCurrentProject: vi.fn(),
    currentUser: { user_id: 'me' },
    showToast: vi.fn(),
  };
  vi.doMock('react-router-dom', () => ({ useNavigate: () => vi.fn() }));
  vi.doMock('../api/client', () => ({
    apiClient: {
      get: vi.fn(() => Promise.reject(new Error('boom'))),
      post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
    },
  }));
  vi.doMock('../utils/routing', () => ({ docUrl: (id: string) => `/docs/${id}` }));
  vi.doMock('../components/ui', () => ({
    Button: (p: { onClick?: () => void; children?: ReactNode }) => createElement('button', { type: 'button', onClick: p.onClick }, p.children),
    IconButton: (p: { title?: string; children?: ReactNode }) => createElement('button', { type: 'button', title: p.title }, p.children),
    Modal: (p: { open?: boolean; children?: ReactNode }) =>
      (p.open ? createElement('div', { 'data-modal': true }, p.children) : null),
    FieldInput: forwardRef<HTMLInputElement, Record<string, unknown>>((props, ref) => createElement('input', { ...props, ref } as never)),
    FieldTextarea: forwardRef<HTMLTextAreaElement, Record<string, unknown>>((props, ref) => createElement('textarea', { ...props, ref } as never)),
  }));
  vi.doMock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
  vi.doMock('../store/app-store', () => ({
    useAppStore: Object.assign(
      (selector: (s: typeof appState) => unknown) => selector(appState),
      { getState: () => appState },
    ),
  }));
  ({ Dashboard } = await import('./Dashboard'));
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  for (const m of ['react-router-dom', '../api/client', '../utils/routing', '../components/ui', '../i18n', '../store/app-store']) vi.doUnmock(m);
});

describe('Dashboard project-list load failure', () => {
  it('toasts failedToLoadProjects and leaves the list untouched', async () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {});
    await act(async () => { root.render(createElement(Dashboard)); });
    expect(appState.showToast).toHaveBeenCalledWith('failedToLoadProjects', 'error');
    expect(appState.setProjects).not.toHaveBeenCalled();
    err.mockRestore();
  });
});

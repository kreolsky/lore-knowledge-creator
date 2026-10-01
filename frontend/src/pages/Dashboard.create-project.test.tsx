/**
 * Dead-button regression: create-project failure must
 * surface an error toast — a rejected POST /projects used to leave the form
 * silently standing (console.error only). The success branch is pinned too:
 * the toast must never fire there. Also pins the POST body contract: the
 * description rides along, trimmed, and an empty one is normalised to null.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, forwardRef, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let Dashboard: typeof import('./Dashboard').Dashboard;
let postMock: ReturnType<typeof vi.fn>;
let navigateMock: ReturnType<typeof vi.fn>;
let showToast: ReturnType<typeof vi.fn>;

beforeEach(async () => {
  vi.resetModules();
  postMock = vi.fn();
  navigateMock = vi.fn();
  showToast = vi.fn();

  vi.doMock('react-router-dom', () => ({ useNavigate: () => navigateMock }));
  vi.doMock('../api/client', () => ({
    apiClient: { get: vi.fn().mockResolvedValue([]), post: postMock, patch: vi.fn(), delete: vi.fn() },
  }));
  vi.doMock('../utils/routing', () => ({ docUrl: (id: string) => `/docs/${id}` }));
  vi.doMock('../components/ui', () => ({
    Button: (p: { type?: string; onClick?: () => void; children?: ReactNode }) =>
      createElement('button', { type: p.type ?? 'button', onClick: p.onClick }, p.children),
    IconButton: (p: { title?: string; children?: ReactNode }) =>
      createElement('button', { type: 'button', title: p.title }, p.children),
    Modal: (p: { open?: boolean; children?: ReactNode }) =>
      (p.open ? createElement('div', { 'data-modal': true }, p.children) : null),
    FieldInput: forwardRef<HTMLInputElement, Record<string, unknown>>((props, ref) =>
      createElement('input', { ...props, ref } as never)),
    FieldTextarea: forwardRef<HTMLTextAreaElement, Record<string, unknown>>((props, ref) =>
      createElement('textarea', { ...props, ref } as never)),
  }));
  vi.doMock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

  const appState = {
    projects: [],
    setProjects: vi.fn(),
    setCurrentProject: vi.fn(),
    currentUser: { user_id: 'u1' },
    showToast,
  };
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
  vi.doUnmock('react-router-dom');
  vi.doUnmock('../api/client');
  vi.doUnmock('../utils/routing');
  vi.doUnmock('../components/ui');
  vi.doUnmock('../i18n');
  vi.doUnmock('../store/app-store');
});

function setNativeValue(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value')!.set!;
  nativeSetter.call(el, value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
}

/** Open the create form, type a name (and optionally a description), submit. */
async function openFormTypeAndSubmit(description?: string) {
  act(() => root.render(createElement(Dashboard)));

  const newProjectBtn = Array.from(container.querySelectorAll('button'))
    .find(b => b.textContent === 'newProject');
  if (!newProjectBtn) throw new Error('new-project button not rendered');
  act(() => { newProjectBtn.dispatchEvent(new MouseEvent('click', { bubbles: true })); });

  const input = container.querySelector('input');
  if (!input) throw new Error('project-name input not rendered');
  act(() => { setNativeValue(input, 'My Project'); });

  if (description !== undefined) {
    const textarea = container.querySelector('textarea');
    if (!textarea) throw new Error('project-description textarea not rendered');
    act(() => { setNativeValue(textarea, description); });
  }

  const form = container.querySelector('form');
  if (!form) throw new Error('create-project form not rendered');
  await act(async () => {
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  });
}

describe('Dashboard create-project — failure surfaces a toast (no dead button)', () => {
  it('a rejecting POST /projects shows the createProjectFailed error toast', async () => {
    postMock.mockRejectedValue(new Error('boom'));
    await openFormTypeAndSubmit();

    expect(showToast).toHaveBeenCalledWith('createProjectFailed', 'error');
  });

  it('success branch stays toast-free and navigates to the new project index doc', async () => {
    postMock.mockResolvedValue({ project_id: 'p1', index_doc_id: 'd1' });
    await openFormTypeAndSubmit();

    expect(showToast).not.toHaveBeenCalled();
    expect(navigateMock).toHaveBeenCalledWith('/docs/d1');
  });
});

describe('Dashboard create-project — POST body carries the description', () => {
  it('sends the description trimmed alongside the name', async () => {
    postMock.mockResolvedValue({ project_id: 'p1', index_doc_id: 'd1' });
    await openFormTypeAndSubmit('  War chronicles  ');

    expect(postMock).toHaveBeenCalledWith('/projects', {
      name: 'My Project',
      description: 'War chronicles',
    });
  });

  it('normalises an empty description to null (a clear, not an empty string)', async () => {
    postMock.mockResolvedValue({ project_id: 'p1', index_doc_id: 'd1' });
    await openFormTypeAndSubmit('   ');

    expect(postMock).toHaveBeenCalledTimes(1);
    const body = postMock.mock.calls[0][1] as { description: string | null };
    expect(body.description == null).toBe(true);
  });
});

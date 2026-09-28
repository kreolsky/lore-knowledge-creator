/**
 * Dead-button regression: create-project failure must
 * surface an error toast — a rejected POST /projects used to leave the form
 * silently standing (console.error only). The success branch is pinned too:
 * the toast must never fire there.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, type ReactNode } from 'react';
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
    FieldInput: (p: { value?: string; onChange: (e: Event) => void; placeholder?: string }) =>
      createElement('input', { value: p.value ?? '', onChange: p.onChange as never, placeholder: p.placeholder }),
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

/** Open the create form, type a name, submit it. */
async function openFormTypeAndSubmit() {
  act(() => root.render(createElement(Dashboard)));

  const newProjectBtn = Array.from(container.querySelectorAll('button'))
    .find(b => b.textContent === 'newProject');
  if (!newProjectBtn) throw new Error('new-project button not rendered');
  act(() => { newProjectBtn.dispatchEvent(new MouseEvent('click', { bubbles: true })); });

  const input = container.querySelector('input');
  if (!input) throw new Error('project-name input not rendered');
  const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
  act(() => {
    nativeSetter.call(input, 'My Project');
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });

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

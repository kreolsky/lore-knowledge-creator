/**
 * Dashboard card inline rename (pencil trigger): commit/cancel semantics,
 * optimistic update with rollback + toast on failure, access gating, and the
 * stopPropagation pins (pencil click / input click must never navigate).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, forwardRef, act, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import type { Project } from '../types';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    project_id: 'p1',
    name: 'Alpha Project',
    status: 'active',
    project_context: '',
    index_doc_id: 'd1',
    voice_recording_doc_id: null,
    last_accessed_doc_id: 'd1',
    owner_id: 'u2',
    owner_name: null,
    is_public: false,
    my_access: 'full',
    created_at: '2026-01-01T00:00:00Z',
    members_count: 0,
    ...overrides,
  };
}

let container: HTMLDivElement;
let root: Root;
let Dashboard: typeof import('./Dashboard').Dashboard;
let patchMock: ReturnType<typeof vi.fn>;
let navigateMock: ReturnType<typeof vi.fn>;
let showToast: ReturnType<typeof vi.fn>;
let appState: {
  projects: Project[];
  setProjects: (projects: Project[]) => void;
  setCurrentProject: ReturnType<typeof vi.fn>;
  currentUser: { user_id: string };
  showToast: ReturnType<typeof vi.fn>;
};

beforeEach(async () => {
  vi.resetModules();
  patchMock = vi.fn();
  navigateMock = vi.fn();
  showToast = vi.fn();

  appState = {
    projects: [makeProject()],
    setProjects: (next: Project[]) => { appState.projects = next; },
    setCurrentProject: vi.fn(),
    currentUser: { user_id: 'u1' },
    showToast,
  };

  vi.doMock('react-router-dom', () => ({ useNavigate: () => navigateMock }));
  vi.doMock('../api/client', () => ({
    apiClient: { get: vi.fn().mockImplementation(() => Promise.resolve(appState.projects)), post: vi.fn(), patch: patchMock, delete: vi.fn() },
  }));
  vi.doMock('../utils/routing', () => ({ docUrl: (id: string) => `/docs/${id}` }));
  vi.doMock('../components/ui', () => ({
    Button: (p: { type?: string; onClick?: () => void; children?: ReactNode }) =>
      createElement('button', { type: p.type ?? 'button', onClick: p.onClick }, p.children),
    IconButton: (p: { title?: string; className?: string; onClick?: (e: unknown) => void; children?: ReactNode }) =>
      createElement('button', { type: 'button', title: p.title, className: p.className, onClick: p.onClick as never }, p.children),
    FieldInput: forwardRef<HTMLInputElement, Record<string, unknown>>((props, ref) =>
      createElement('input', { ...props, ref } as never)),
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
  vi.doUnmock('react-router-dom');
  vi.doUnmock('../api/client');
  vi.doUnmock('../utils/routing');
  vi.doUnmock('../components/ui');
  vi.doUnmock('../i18n');
  vi.doUnmock('../store/app-store');
});

function renderDashboard() {
  act(() => { root.render(createElement(Dashboard)); });
}

function findPencil(): HTMLButtonElement {
  const pencil = Array.from(container.querySelectorAll('button'))
    .find(b => b.title === 'rename');
  if (!pencil) throw new Error('rename pencil not rendered');
  return pencil as HTMLButtonElement;
}

function getRenameInput(): HTMLInputElement {
  const input = container.querySelector('input');
  if (!input) throw new Error('rename input not rendered');
  return input as HTMLInputElement;
}

function setNativeValue(input: HTMLInputElement, value: string) {
  const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!;
  nativeSetter.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

function openRename() {
  act(() => { findPencil().dispatchEvent(new MouseEvent('click', { bubbles: true })); });
  return getRenameInput();
}

describe('Dashboard card inline rename', () => {
  it('pencil opens a focused, fully-selected input; type + Enter PATCHes, updates the card, never navigates', async () => {
    patchMock.mockResolvedValue(makeProject({ name: 'Beta Project' }));
    renderDashboard();

    const input = openRename();
    expect(document.activeElement).toBe(input);
    expect(input.selectionStart).toBe(0);
    expect(input.selectionEnd).toBe('Alpha Project'.length);

    await act(async () => {
      setNativeValue(input, 'Beta Project');
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    });

    expect(patchMock).toHaveBeenCalledWith('/projects/p1', { name: 'Beta Project' });
    expect(navigateMock).not.toHaveBeenCalled();

    renderDashboard();
    expect(container.textContent).toContain('Beta Project');
    expect(container.querySelector('input')).toBeNull();
  });

  it('a rejecting PATCH reverts the card to the old name and shows the error toast', async () => {
    patchMock.mockRejectedValue(new Error('boom'));
    renderDashboard();

    const input = openRename();
    await act(async () => {
      setNativeValue(input, 'Gamma Project');
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    });

    expect(patchMock).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith('renameProjectFailed', 'error');

    renderDashboard();
    expect(container.textContent).toContain('Alpha Project');
    expect(container.textContent).not.toContain('Gamma Project');
  });

  it('Esc closes the input with no PATCH', () => {
    renderDashboard();

    const input = openRename();
    act(() => {
      setNativeValue(input, 'Delta Project');
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    });

    expect(container.querySelector('input')).toBeNull();
    expect(patchMock).not.toHaveBeenCalled();
  });

  it('empty, unchanged, or 1-char values are silent no-ops — no PATCH', () => {
    renderDashboard();

    let input = openRename();
    act(() => {
      setNativeValue(input, '');
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    });
    expect(patchMock).not.toHaveBeenCalled();

    input = openRename();
    act(() => {
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    });
    expect(patchMock).not.toHaveBeenCalled();

    input = openRename();
    act(() => {
      setNativeValue(input, 'A');
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    });
    expect(patchMock).not.toHaveBeenCalled();
    expect(container.querySelector('input')).toBeNull();
  });

  it('a readonly project renders no pencil and opens nothing on title dblclick', () => {
    appState.projects = [makeProject({ my_access: 'readonly' })];
    renderDashboard();

    const pencil = Array.from(container.querySelectorAll('button')).find(b => b.title === 'rename');
    expect(pencil).toBeUndefined();

    const title = container.querySelector('h3');
    if (!title) throw new Error('card title not rendered');
    act(() => { title.dispatchEvent(new MouseEvent('dblclick', { bubbles: true })); });
    expect(container.querySelector('input')).toBeNull();
  });

  it('a click inside the open input does not navigate the card', () => {
    renderDashboard();

    const input = openRename();
    act(() => { input.dispatchEvent(new MouseEvent('click', { bubbles: true })); });

    expect(navigateMock).not.toHaveBeenCalled();
    expect(container.querySelector('input')).not.toBeNull();
  });
});

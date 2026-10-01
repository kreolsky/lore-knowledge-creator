/**
 * Dashboard card edit modal (pencil trigger, my_access === 'full' only):
 * prefilled name + description, Save PATCHes both, optimistic update with
 * rollback + toast on failure, no-op guards, and the stopPropagation pin
 * (pencil click must never navigate the card). Replaces the inline-rename
 * test — the inline input itself is gone by request (plan project-description).
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
    projects: [makeProject({ description: 'Old description' })],
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
    Modal: (p: { open?: boolean; title?: string; children?: ReactNode }) =>
      (p.open ? createElement('div', { 'data-modal': p.title }, p.children) : null),
    FieldInput: forwardRef<HTMLInputElement, Record<string, unknown>>((props, ref) =>
      createElement('input', { ...props, ref } as never)),
    FieldTextarea: forwardRef<HTMLTextAreaElement, Record<string, unknown>>((props, ref) =>
      createElement('textarea', { ...props, ref } as never)),
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
    .find(b => b.title === 'editProject');
  if (!pencil) throw new Error('edit pencil not rendered');
  return pencil as HTMLButtonElement;
}

function getModal(): HTMLDivElement {
  const modal = container.querySelector('[data-modal]');
  if (!modal) throw new Error('edit modal not rendered');
  return modal as HTMLDivElement;
}

function getNameInput(): HTMLInputElement {
  const input = getModal().querySelector('input');
  if (!input) throw new Error('name input not rendered in modal');
  return input as HTMLInputElement;
}

function getDescriptionTextarea(): HTMLTextAreaElement {
  const textarea = getModal().querySelector('textarea');
  if (!textarea) throw new Error('description textarea not rendered in modal');
  return textarea as HTMLTextAreaElement;
}

function setNativeValue(el: HTMLInputElement | HTMLTextAreaElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value')!.set!;
  nativeSetter.call(el, value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
}

/** Pencil click → open modal → return its name input. */
function openModal(): HTMLInputElement {
  act(() => { findPencil().dispatchEvent(new MouseEvent('click', { bubbles: true })); });
  return getNameInput();
}

function submitModal() {
  const form = getModal().querySelector('form');
  if (!form) throw new Error('edit form not rendered in modal');
  act(() => { form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })); });
}

describe('Dashboard card edit modal', () => {
  it('pencil opens a prefilled modal; edit name + description, Save PATCHes both, updates the card, never navigates', async () => {
    patchMock.mockResolvedValue(makeProject({ name: 'Beta Project', description: 'New description' }));
    renderDashboard();

    const input = openModal();
    expect(input.value).toBe('Alpha Project');
    expect(getDescriptionTextarea().value).toBe('Old description');

    await act(async () => {
      setNativeValue(input, 'Beta Project');
      setNativeValue(getDescriptionTextarea(), 'New description');
      submitModal();
    });

    expect(patchMock).toHaveBeenCalledWith('/projects/p1', {
      name: 'Beta Project',
      description: 'New description',
    });
    expect(navigateMock).not.toHaveBeenCalled();

    renderDashboard();
    expect(container.textContent).toContain('Beta Project');
    expect(container.textContent).toContain('New description');
    expect(container.querySelector('[data-modal]')).toBeNull();
  });

  it('clearing the description PATCHes description: null (a clear, not an empty string)', async () => {
    patchMock.mockResolvedValue(makeProject({ name: 'Alpha Project', description: null }));
    renderDashboard();

    const input = openModal();
    await act(async () => {
      setNativeValue(input, 'Alpha Project');
      setNativeValue(getDescriptionTextarea(), '   ');
      submitModal();
    });

    expect(patchMock).toHaveBeenCalledWith('/projects/p1', {
      name: 'Alpha Project',
      description: null,
    });
  });

  it('a rejecting PATCH reverts the card to the old name/description and shows the error toast', async () => {
    patchMock.mockRejectedValue(new Error('boom'));
    renderDashboard();

    const input = openModal();
    await act(async () => {
      setNativeValue(input, 'Gamma Project');
      setNativeValue(getDescriptionTextarea(), 'Should not stick');
      submitModal();
    });

    expect(patchMock).toHaveBeenCalledTimes(1);
    expect(showToast).toHaveBeenCalledWith('updateProjectFailed', 'error');

    renderDashboard();
    expect(container.textContent).toContain('Alpha Project');
    expect(container.textContent).not.toContain('Gamma Project');
    expect(container.textContent).not.toContain('Should not stick');
  });

  it('Cancel closes the modal with no PATCH', () => {
    renderDashboard();

    openModal();
    const cancel = Array.from(getModal().querySelectorAll('button'))
      .find(b => b.textContent === 'cancel');
    if (!cancel) throw new Error('cancel button not rendered');
    act(() => { cancel.dispatchEvent(new MouseEvent('click', { bubbles: true })); });

    expect(container.querySelector('[data-modal]')).toBeNull();
    expect(patchMock).not.toHaveBeenCalled();
  });

  it('unchanged values or a 1-char name are silent no-ops — no PATCH', () => {
    renderDashboard();

    let input = openModal();
    act(() => { submitModal(); });
    expect(patchMock).not.toHaveBeenCalled();
    expect(container.querySelector('[data-modal]')).toBeNull();

    input = openModal();
    act(() => {
      setNativeValue(input, 'A');
      submitModal();
    });
    expect(patchMock).not.toHaveBeenCalled();
  });

  it('a readonly project renders no pencil', () => {
    appState.projects = [makeProject({ my_access: 'readonly', description: null })];
    renderDashboard();

    const pencil = Array.from(container.querySelectorAll('button')).find(b => b.title === 'editProject');
    expect(pencil).toBeUndefined();
  });

  it('a pencil click does not navigate the card', () => {
    renderDashboard();

    act(() => { findPencil().dispatchEvent(new MouseEvent('click', { bubbles: true })); });

    expect(navigateMock).not.toHaveBeenCalled();
    expect(container.querySelector('[data-modal]')).not.toBeNull();
  });
});

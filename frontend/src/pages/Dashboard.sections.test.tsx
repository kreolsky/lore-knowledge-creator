/**
 * Dashboard sections: own projects under "My projects" (with the create
 * button in that heading row), every other visible project under "Shared
 * with me" with an access badge per level; the shared section is absent
 * when nothing is shared. Card anatomy: description under the title
 * (line-clamped) and the one meta line (owner | members for others'
 * projects, members only for own, nothing at 0 members).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, forwardRef, act, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import type { AccessLevel, Project } from '../types';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function makeProject(id: string, owner: string, access: AccessLevel, overrides: Partial<Project> = {}): Project {
  return {
    project_id: id,
    name: `Project ${id}`,
    status: 'active',
    project_context: '',
    index_doc_id: `d-${id}`,
    voice_recording_doc_id: null,
    last_accessed_doc_id: null,
    owner_id: owner,
    owner_name: owner,
    is_public: false,
    my_access: access,
    created_at: '2026-01-01T00:00:00Z',
    members_count: 0,
    ...overrides,
  };
}

let container: HTMLDivElement;
let root: Root;
let Dashboard: typeof import('./Dashboard').Dashboard;
let appState: { projects: Project[]; setProjects: (p: Project[]) => void; setCurrentProject: () => void; currentUser: { user_id: string }; showToast: () => void };

beforeEach(async () => {
  vi.resetModules();
  appState = {
    projects: [],
    setProjects: (p) => { appState.projects = p; },
    setCurrentProject: vi.fn(),
    currentUser: { user_id: 'me' },
    showToast: vi.fn(),
  };
  vi.doMock('react-router-dom', () => ({ useNavigate: () => vi.fn() }));
  vi.doMock('../api/client', () => ({
    apiClient: { get: vi.fn().mockImplementation(() => Promise.resolve(appState.projects)), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
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

const section = (name: string) => container.querySelector(`[data-section="${name}"]`);
const cardNames = (el: Element | null) => Array.from(el?.querySelectorAll('h3') ?? []).map(h => h.firstChild?.textContent);

describe('Dashboard own / shared sections', () => {
  it('splits by owner, puts the create button in the own heading, badges every shared level', () => {
    appState.projects = [
      makeProject('a', 'me', 'full'),
      makeProject('b', 'other', 'full'),
      makeProject('c', 'other', 'commentator'),
      makeProject('d', 'other', 'readonly'),
    ];
    act(() => root.render(createElement(Dashboard)));

    const own = section('own');
    const shared = section('shared');
    expect(own?.querySelector('h2')?.textContent).toBe('myProjects');
    expect(own?.querySelector('h2')?.parentElement?.querySelector('button')?.textContent).toBe('newProject');
    expect(cardNames(own)).toEqual(['Project a']);
    expect(own?.querySelector('[data-access-badge]')).toBeNull();

    expect(shared?.querySelector('h2')?.textContent).toBe('sharedProjects');
    expect(cardNames(shared)).toEqual(['Project b', 'Project c', 'Project d']);
    const badges = Array.from(shared?.querySelectorAll('[data-access-badge]') ?? []);
    expect(badges.map(b => [b.getAttribute('data-access-badge'), b.textContent?.trim()])).toEqual([
      ['full', 'fullAccessTag'],
      ['commentator', 'notesAccess'],
      ['readonly', 'roAccess'],
    ]);
    expect(badges.map(b => b.className.includes('text-green'))).toEqual([true, false, false]);
    expect(badges[1].className).toContain('sticky-yellow-dark');
    expect(badges[2].className).toContain('text-red');
  });

  it('renders no shared section when nothing is shared, and the empty hint under own', () => {
    act(() => root.render(createElement(Dashboard)));

    expect(section('shared')).toBeNull();
    expect(section('own')?.textContent).toContain('noProjectsYet');
  });
});

describe('Dashboard card meta line + description', () => {
  it("others' project: owner | members when N>0, owner only (no separator) when N=0", () => {
    appState.projects = [
      makeProject('c', 'other', 'full', { members_count: 2 }),
      makeProject('d', 'other', 'full', {}),
    ];
    act(() => root.render(createElement(Dashboard)));

    const metas = Array.from(section('shared')?.querySelectorAll('[data-meta]') ?? []);
    expect(metas).toHaveLength(2);
    expect(metas[0].textContent).toContain('other');
    expect(metas[0].textContent).toContain('2');
    expect(metas[0].textContent).toContain('|');
    expect(metas[1].textContent).toContain('other');
    expect(metas[1].textContent).not.toContain('|');
  });

  it('own project: members only when N>0, no meta line at all when N=0', () => {
    appState.projects = [
      makeProject('a', 'me', 'full', { members_count: 2 }),
      makeProject('b', 'me', 'full', {}),
    ];
    act(() => root.render(createElement(Dashboard)));

    const metas = Array.from(section('own')?.querySelectorAll('[data-meta]') ?? []);
    expect(metas).toHaveLength(1);
    expect(metas[0].textContent).toContain('2');
    expect(metas[0].textContent).not.toContain('me');
    expect(metas[0].textContent).not.toContain('|');
  });

  it('renders the description under the title only when non-empty, clamped to 5 lines', () => {
    appState.projects = [
      makeProject('a', 'me', 'full', { description: 'Line one\nLine two' }),
      makeProject('b', 'me', 'full', { description: null }),
    ];
    act(() => root.render(createElement(Dashboard)));

    const own = section('own');
    const descriptions = Array.from(own?.querySelectorAll('[data-description]') ?? []);
    expect(descriptions).toHaveLength(1);
    expect(descriptions[0].textContent).toBe('Line one\nLine two');
    expect(descriptions[0].className).toContain('line-clamp-5');
    expect(descriptions[0].className).toContain('whitespace-pre-line');
    // The description sits below the title row, above the meta line.
    const title = own?.querySelector('h3');
    expect(title && descriptions[0].compareDocumentPosition(title) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
  });
});

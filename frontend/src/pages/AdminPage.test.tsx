/** AdminPage — tab-less section: sections in the aside, full lists in the center.
 *
 * Pins the admin-section-as-tab plan's contract:
 * - the shell has NO top tabs; the aside lists Projects / Users / Embeddings
 *   plus (admin-only) the five settings tabs and Skills as rows, the section
 *   restores from the user's saved global prefs (Users when nothing/invalid
 *   is saved; a moderator's stale settings or skills tab falls back to Users);
 * - the center shows the active section's FULL list: project cards (with the
 *   owner chip on the collapsed card, repeated by the expanded owner row)
 *   under the projects filter; the add-user form, a "Users"
 *   heading, the users filter and user cards (with email);
 * - the expanded member list pins a non-editable owner row first and offers
 *   `Owner` on member rows: picking it POSTs /admin/projects/{id}/owner, the
 *   old owner takes the target's exact slot (no-jump), and a refusal toasts
 *   with the rows unchanged;
 * - the projects filter is SERVER-side (debounced q, offset reset to 0) and the
 *   list pages by sentinel intersection in 100s; Escape clears + blurs either
 *   filter; the users filter matches client-side by name OR email;
 * - section choice persists per user via PUT /preferences/_global, and the
 *   aside width is ONE field with the project sidebar (panelWidths.left) — a
 *   section drag PUTs it, a project-dragged width seeds the aside (real
 *   ui-store runs; app bridge registered by the harness).
 *
 * Harness: manual createRoot + act (mirrors Layout.test.tsx). apiClient is
 * mocked with in-memory fixtures (URL-routed: /admin/projects honours q /
 * offset / limit like the backend); EmbeddingsTab and UserControls are markers
 * (their polling/avatar chrome is not under test here).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// WHY 15s: a full AdminPage render plus real-timer debounce sleeps runs ~0.9s
// on an idle box and ~4× that on the CI runner — the 5s default left no margin,
// and one timed-out test leaves its render running into the next ones.
vi.setConfig({ testTimeout: 15_000 });

let AdminPage: typeof import('./AdminPage').AdminPage;
let useUIStore: (typeof import('../store/ui-store'))['useUIStore'];
let chatState: Record<string, unknown>;
let container: HTMLDivElement;
let root: Root;
let appState: Record<string, unknown>;
let navigate: ReturnType<typeof vi.fn>;
let apiGet: ReturnType<typeof vi.fn>;
let apiPatch: ReturnType<typeof vi.fn>;
let apiPost: ReturnType<typeof vi.fn>;
let apiDelete: ReturnType<typeof vi.fn>;
let apiPut: ReturnType<typeof vi.fn>;

const USERS = [
  { user_id: 'u1', name: 'Alice Admin', email: 'alice@x.test', role: 'admin', has_pin: false },
  { user_id: 'u2', name: 'Bob', email: 'bob@x.test', role: 'user', has_pin: false },
  { user_id: 'u3', name: 'Carol', email: 'carol@x.test', role: 'user', has_pin: false, moderator_id: 'u2', moderator_name: 'Bob', created_by: 'u1', created_by_name: 'Alice Admin' },
];
/** 151 projects: 2 named + 1 locked + 148 generated. Page 1 (offset 0, limit
 * 100) ends before "Project 101", so sentinel paging is observable via
 * "Project 150". */
const ALL_PROJECTS = [
  { project_id: 'p1', name: 'Alpha Project', owner_id: 'u1', owner_name: 'Alice Admin', audiences: [], my_access: 'full' },
  { project_id: 'p2', name: 'Beta Project', owner_id: 'u2', owner_name: 'Bob', audiences: [{ subject: 'public', access_level: 'readonly' }], my_access: 'readonly' },
  { project_id: 'p_locked', name: 'Locked Project', owner_id: 'u2', owner_name: 'Bob', audiences: [], my_access: null },
  ...Array.from({ length: 148 }, (_, i) => ({
    project_id: `p${i + 3}`,
    name: `Project ${String(i + 3).padStart(3, '0')}`,
    owner_id: 'u1',
    owner_name: 'Alice Admin',
    audiences: [],
    my_access: 'full',
  })),
];

const I18N: Record<string, string> = {
  projects: 'Projects',
  users: 'Users',
  embeddings: 'Embeddings',
  addUser: 'Add user',
  add: 'Add',
  filterProjects: 'Filter projects',
  filterUsers: 'Filter users',
  adminListLoadFailed: 'Failed to load the list',
  adminRole: 'Admin',
  userRole: 'User',
  moderatorRole: 'Moderator',
  role: 'Role',
  ownerRole: 'Owner',
  failedToUpdateAccess: 'Failed to update access',
  openProject: 'Open project',
  noProjectAccess: 'No access',
  publicLabel: 'Public',
  inviteLink: 'Invite link',
  inviteLinkCopied: 'Invite link copied',
  failedToCreateInvite: 'Failed to create the invite link',
  invitedBy: 'invited by',
  group: 'group',
  noGroup: 'No group',
  editNameEmail: 'Edit name / email',
  deleteUser: 'Delete user',
  newPassword: 'New password',
  generatePasswordTitle: 'Generate password',
  clearPassword: 'Clear password',
  copied: 'Copied!',
  save: 'Save',
  saved: 'Saved',
  cancel: 'Cancel',
  adminInfo: 'Info',
  settingsModels: 'Models & APIs',
  settingsAgent: 'Agent',
  settingsTools: 'Tools',
  settingsSearch: 'Search',
  settingsStorage: 'Storage & Jobs',
  skills: 'Skills',
  adminModelAccess: 'Model access',
  adminGroups: 'Groups',
};
const tFn = (k: string) => I18N[k] ?? k;

beforeEach(async () => {
  vi.resetModules();
  appState = { currentUser: { user_id: 'u1', name: 'Alice Admin', role: 'admin', is_admin: true, can_manage_users: true, can_publish_project: true }, showToast: vi.fn(), sectionOrigin: null, sectionCrumb: null };
  appState.setSectionCrumb = vi.fn((label: string | null) => { appState.sectionCrumb = label; });
  navigate = vi.fn();
  apiPatch = vi.fn(() => Promise.resolve({}));
  apiPost = vi.fn(() => Promise.resolve({}));
  apiPut = vi.fn(() => Promise.resolve({}));
  apiGet = vi.fn((url: string) => {
    const [path, search = ''] = url.split('?');
    if (path === '/admin/users') {
      return Promise.resolve(USERS);
    }
    if (path === '/admin/projects') {
      const params = new URLSearchParams(search);
      const q = (params.get('q') ?? '').toLowerCase();
      const offset = Number(params.get('offset') ?? '0');
      const limit = Number(params.get('limit') ?? '100');
      const filtered = ALL_PROJECTS.filter(p => !q || p.name.toLowerCase().includes(q));
      return Promise.resolve(filtered.slice(offset, offset + limit));
    }
    return Promise.resolve([]);
  });

  vi.doMock('react-router-dom', () => ({
    useNavigate: () => navigate,
    // Link stub — ProjectCard's "open project" anchor (no router context here).
    Link: ({ to, children, ...rest }: { to: string; children: ReactNode }) =>
      createElement('a', { href: to, ...rest }, children),
  }));
  vi.doMock('../api/client', async () => {
    // Spread the ACTUAL module so exported classes (HttpError) keep their
    // identity — the toast-detail test constructs the real one — and mock
    // only the transport.
    const actual = await vi.importActual<Record<string, unknown>>('../api/client');
    return {
      ...actual,
      apiClient: {
        get: apiGet,
        post: apiPost,
        patch: apiPatch,
        put: apiPut,
        delete: (apiDelete = vi.fn(() => Promise.resolve({}))),
      },
    };
  });
  chatState = { modelsLoaded: true };
  vi.doMock('../store/chat-store', () => ({
    useChatStore: { setState: (patch: Record<string, unknown>) => { chatState = { ...chatState, ...patch }; } },
  }));
  vi.doMock('../store/app-store', () => {
    // Function + getState — AdminPage's fire-and-forget toasts go through
    // useAppStore.getState(), the hook reads via the selector.
    const useAppStore = (sel: (s: Record<string, unknown>) => unknown) => sel(appState);
    (useAppStore as unknown as { getState: () => Record<string, unknown> }).getState = () => appState;
    return { useAppStore };
  });
  vi.doMock('../i18n', () => ({
    t: tFn,
    useTranslation: () => ({ t: tFn }),
  }));
  vi.doMock('../components/UserControls', () => ({
    UserControls: () => createElement('div', { 'data-testid': 'user-controls' }),
  }));
  vi.doMock('../components/admin/EmbeddingsTab', () => ({
    EmbeddingsTab: () => createElement('div', { 'data-testid': 'embeddings-tab' }),
  }));
  vi.doMock('../components/admin/SettingsSection', () => ({
    SettingsSection: ({ tab }: { tab: string }) =>
      createElement('div', { 'data-testid': `settings-section-${tab}` }),
  }));
  vi.doMock('../components/admin/SkillsSection', () => ({
    SkillsSection: () => createElement('div', { 'data-testid': 'skills-section' }),
  }));

  ({ AdminPage } = await import('./AdminPage'));

  // REAL ui-store (AdminPage reads/writes its global-prefs slice). The bridge is
  // what lets triggerSaveGlobalPrefs reach apiClient.put — DocumentTree.reveal
  // test pattern.
  const ui = await import('../store/ui-store');
  useUIStore = ui.useUIStore;
  ui.registerAppBridge({
    getAppContext: () => ({
      currentUser: (appState.currentUser as { user_id: string }) ?? null,
      currentProject: null,
      currentDocument: null,
    }),
    showToast: () => {},
  });
  // Reset the per-user prefs slice between tests (module is re-imported fresh
  // by resetModules, but seed explicitly for readability).
  useUIStore.setState({ adminSectionTab: null });

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('react-router-dom');
  vi.doUnmock('../api/client');
  vi.doUnmock('../store/app-store');
  vi.doUnmock('../i18n');
  vi.doUnmock('../components/UserControls');
  vi.doUnmock('../components/admin/EmbeddingsTab');
  vi.doUnmock('../components/admin/SettingsSection');
  vi.doUnmock('../components/admin/SkillsSection');
  vi.unstubAllGlobals();
});

async function render() {
  await act(async () => { root.render(createElement(AdminPage)); });
  // flush the mount-effect fetch microtasks
  await act(async () => {});
}

function aside() { return container.querySelector('aside'); }
function main() { return container.querySelector('main'); }

function clickButtonWithText(scope: ParentNode | null, text: string) {
  const btn = Array.from(scope?.querySelectorAll('button') ?? [])
    .find(b => b.textContent?.includes(text));
  expect(btn, `button containing "${text}"`).toBeDefined();
  act(() => btn!.click());
}

/** Section rows live in the aside (`.doc-item` with title = label). Exact
 * title, not substring: the "myProjects" back button also contains "Projects". */
function clickSection(label: string) {
  const btn = aside()?.querySelector<HTMLButtonElement>(`button.doc-item[title="${label}"]`);
  expect(btn, `section row "${label}"`).toBeDefined();
  act(() => btn!.click());
}

/** React-controlled input: native value setter + input event (this harness is
 * manual createRoot + act, so no testing-library fireEvent). */
async function typeInto(input: HTMLInputElement, value: string) {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!;
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

/** Dropdown triggers carry `title` (Role / group); the listbox mounts only
 * while open, so read options by clicking the trigger first. */
function dropdownTriggers(scope: ParentNode, title: string): HTMLButtonElement[] {
  return Array.from(scope.querySelectorAll(`button[title="${title}"]`));
}

function projectsCalls(): string[] {
  return apiGet.mock.calls.map(c => c[0] as string).filter(u => u.startsWith('/admin/projects'));
}

/** Projects is not the initial section: its pipeline starts on the first click. */
async function openProjects() {
  await render();
  clickSection('Projects');
  await act(async () => {});
}

describe('AdminPage — sections (initial = Users)', () => {

  it('center shows the FULL project card list, each collapsed card chipping its owner', async () => {
    await openProjects();
    expect(main()?.textContent).toContain('Alpha Project');
    expect(main()?.textContent).toContain('Beta Project');
    // Owner chip on the COLLAPSED card — readable without expanding.
    const card = (title: string) =>
      Array.from(main()!.querySelectorAll('section'))
        .find(s => s.textContent!.startsWith(title))!;
    expect(card('Alpha Project').textContent).toContain('Alice Admin');
    expect(card('Beta Project').textContent).toContain('Bob');
    expect(main()?.querySelectorAll('section').length).toBe(100);
  });

  it('the open-project link is disabled for projects the viewer cannot enter (my_access null)', async () => {
    await openProjects();
    const card = (title: string) =>
      Array.from(main()!.querySelectorAll('section'))
        .find(s => s.textContent!.startsWith(title))!;
    // No access → no anchor at all; a disabled placeholder carries the reason.
    expect(card('Locked Project').querySelector('a[href="/projects/p_locked"]')).toBeNull();
    expect(card('Locked Project').querySelector('span[title="No access"]')).not.toBeNull();
    // Access (owner, member, or audience row) → the link stays.
    expect(card('Alpha Project').querySelector('a[href="/projects/p1"]')).not.toBeNull();
    expect(card('Beta Project').querySelector('a[href="/projects/p2"]')).not.toBeNull();
    // Entering a project must NOT replace the admin list: the link opens a
    // NEW window (target=_blank, noopener) so the panel stays behind it.
    const enter = card('Alpha Project').querySelector('a[href="/projects/p1"]')!;
    expect(enter.getAttribute('target')).toBe('_blank');
    expect(enter.getAttribute('rel')).toContain('noopener');
    // Audience editing lives in the settings panel only: the card carries a
    // PASSIVE badge (a span), never a toggle button — for admins too.
    expect(Array.from(card('Beta Project').querySelectorAll('button')).some(b => b.textContent === 'Public')).toBe(false);
    expect(card('Beta Project').textContent).toContain('Public');
  });
});

describe('AdminPage — per-user persistence: last section + shared width (panelWidths.left)', () => {
  /** Last global-prefs PUT body, or fail — used to read what the page persisted. */
  function lastGlobalPrefs(): Record<string, unknown> {
    const put = apiPut.mock.calls.filter(c => c[0] === '/preferences/_global').at(-1);
    expect(put, 'PUT /preferences/_global fired').toBeDefined();
    return (put![1] as { preferences: Record<string, unknown> }).preferences;
  }

  it('opens on the user\'s saved section and starts its pipeline (projects)', async () => {
    useUIStore.setState({ adminSectionTab: 'projects' });
    await render();
    expect(aside()?.querySelector('button.doc-item.active')?.textContent).toBe('Projects');
    // Restored Projects behaves like a click: the pipeline fetches page 0.
    expect(projectsCalls()[0]).toBe('/admin/projects?offset=0&limit=100');
  });

  it('aside seeds at the shared width (panelWidths.left); a drag PUTs the new width', async () => {
    useUIStore.setState({ panelWidths: { left: 333, right: null } });
    await render();
    // Same field the project sidebar reads — project → section has no jump.
    expect(aside()?.style.width).toBe('333px');

    const handle = container.querySelector('.resizer') as HTMLElement;
    expect(handle, 'resizer handle').toBeDefined();
    await act(async () => {
      handle.dispatchEvent(new MouseEvent('pointerdown', { clientX: 300, bubbles: true }));
      document.dispatchEvent(new MouseEvent('pointermove', { clientX: 360, bubbles: true })); // +60 → 393
      document.dispatchEvent(new MouseEvent('pointerup', { clientX: 360, bubbles: true }));
    });
    expect(lastGlobalPrefs().panelWidths).toEqual({ left: 393, right: null });
  });

  it('no width ever chosen → the 220 default (same as the project sidebar default)', async () => {
    await render();
    expect(aside()?.style.width).toBe('220px');
  });
});

describe('AdminPage — moderator view (can_manage_users && !is_admin)', () => {

  async function renderAsModerator() {
    appState.currentUser = { user_id: 'm1', name: 'Mia Mod', role: 'moderator', can_manage_users: true, is_admin: false };
    await render();
    await act(async () => {});
  }

  it('removing MYSELF from a project disables its open button at once', async () => {
    const project = {
      project_id: 'p9', name: 'Group Project', owner_id: 'u3', owner_name: 'Carol',
      audiences: [], my_access: 'commentator',
    };
    (apiGet as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      const [path] = url.split('?');
      if (path === '/admin/users') return Promise.resolve(USERS);
      if (path === '/admin/projects') return Promise.resolve([project]);
      if (path === '/admin/projects/p9/members') return Promise.resolve({ m1: 'commentator' });
      // my_access comes from the SERVER after a self-membership change — the
      // re-read of the project, never a local audiences fallback.
      if (path === '/projects/p9') return Promise.resolve({ project: { project_id: 'p9', my_access: null } });
      return Promise.resolve([]);
    });
    await renderAsModerator();
    clickSection('Projects');
    await act(async () => {});
    const card = main()!.querySelector('section')!;
    // The card badge is passive for everyone: no Public button (nothing to
    // 403 on click), and a project with no 'public' row carries no badge.
    expect(Array.from(card.querySelectorAll('button')).some(b => b.textContent === 'Public')).toBe(false);
    expect(card.textContent).not.toContain('Public');
    // Member (commentator) → the open link is live.
    expect(card.querySelector('a[href="/projects/p9"]')).not.toBeNull();
    act(() => { card.querySelector<HTMLButtonElement>('button')!.click(); });
    await act(async () => {});
    await act(async () => { (card.querySelector('button[title="Remove access"]') as HTMLButtonElement).click(); });
    expect(apiDelete).toHaveBeenCalledWith('/admin/projects/p9/members/m1');
    expect(apiGet).toHaveBeenCalledWith('/projects/p9');
    // Server says no access left → disabled immediately, no reload.
    expect(card.querySelector('a[href="/projects/p9"]')).toBeNull();
    expect(card.querySelector('span[title="No access"]')).not.toBeNull();
  });
});

describe('AdminPage — admin-only surfaces', () => {
  function localCard(email: string): HTMLElement {
    const el = Array.from(main()?.querySelectorAll('section') ?? [])
      .find(sec => sec.textContent?.includes(email));
    expect(el, `card for ${email}`).toBeDefined();
    return el!;
  }

  it('user edit panel has role + moderator selects for an admin, and no plain-text role chip in the panel', async () => {
    await render();
    clickSection('Users');
    await act(async () => {});
    act(() => { (localCard('bob@x.test').querySelector('button[title="Edit name / email"]') as HTMLButtonElement).click(); });
    await act(async () => {});
    const cardEl = localCard('bob@x.test');
    expect(dropdownTriggers(cardEl, 'Role')).toHaveLength(1);
    expect(dropdownTriggers(cardEl, 'group')).toHaveLength(1);
    // The role lives ONLY in the select: the "| role" chip stays in the
    // collapsed row header, never inside the edit panel.
    const panel = cardEl.querySelector('div.border-t')!;
    expect(panel.textContent).not.toContain('|');
    // Autofill guard: email+password side by side reads as a LOGIN form to
    // the browser, which fills the ADMIN's saved credentials into another
    // user's panel — the PATCH then carries a foreign email → 409.
    const pwd = panel.querySelector('input[type=password]')!;
    expect(pwd.getAttribute('autocomplete')).toBe('new-password');
    const email = panel.querySelector('input[type=email]')!;
    expect(email.getAttribute('autocomplete')).toBe('off');
  });

  it('own card: no role/moderator selects in the edit panel (self role change is refused server-side)', async () => {
    await render();
    clickSection('Users');
    await act(async () => {});
    act(() => { (localCard('alice@x.test').querySelector('button[title="Edit name / email"]') as HTMLButtonElement).click(); });
    await act(async () => {});
    expect(dropdownTriggers(localCard('alice@x.test'), 'Role')).toHaveLength(0);
    expect(dropdownTriggers(localCard('alice@x.test'), 'group')).toHaveLength(0);
  });

  it('failed save toasts the server refusal detail verbatim, not only the generic line', async () => {
    const { HttpError } = await vi.importActual<typeof import('../api/client')>('../api/client');
    await render();
    clickSection('Users');
    await act(async () => {});
    (apiPatch as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new HttpError(400, '{"detail":"Cannot change your own role"}'));
    act(() => { (localCard('bob@x.test').querySelector('button[title="Edit name / email"]') as HTMLButtonElement).click(); });
    await act(async () => {});
    const nameInput = localCard('bob@x.test').querySelectorAll('input')[0];
    await typeInto(nameInput, 'Bobby');
    clickButtonWithText(localCard('bob@x.test'), 'Save');
    await act(async () => {});
    expect(appState.showToast).toHaveBeenCalledWith('Cannot change your own role');
    // A refused save keeps the draft and never claims Saved.
    expect(localCard('bob@x.test').querySelectorAll('input')[0].value).toBe('Bobby');
    expect(localCard('bob@x.test').textContent).not.toContain('Saved');

    // pydantic 422 shape: detail is an ARRAY — the short-password refusal.
    (apiPatch as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new HttpError(422, '{"detail":[{"type":"string_too_short","loc":["body","password"],"msg":"String should have at least 6 characters"}]}'));
    clickButtonWithText(localCard('bob@x.test'), 'Save');
    await act(async () => {});
    expect(appState.showToast).toHaveBeenCalledWith('String should have at least 6 characters');
  });
});

describe('AdminPage — Model access: Hidden · Private · Public selector', () => {
  const MODELS = [
    { id: 'deepseek/flash', in_gateway: true, is_default: true, subjects: [] },
    { id: 'openai/luna', in_gateway: true, is_default: false, subjects: ['group:g1'] },
  ];

  function modelRow(id: string): HTMLElement {
    const el = main()?.querySelector<HTMLElement>(`section[data-model-id="${id}"]`);
    expect(el, `model row ${id}`).toBeTruthy();
    return el!;
  }

  function stop(row: HTMLElement, label: string): HTMLButtonElement {
    const btn = Array.from(row.querySelectorAll<HTMLButtonElement>('[role=radio]'))
      .find(b => b.textContent === label);
    expect(btn, `stop "${label}"`).toBeDefined();
    return btn!;
  }

  async function openModels() {
    const base = apiGet.getMockImplementation() as (url: string) => Promise<unknown>;
    apiGet.mockImplementation((url: string) =>
      url === '/admin/models' ? Promise.resolve(MODELS) : base(url));
    await render();
    clickSection('Model access');
    await act(async () => {});
  }

  it('clicking Hidden PUTs the model\'s subjects as ["hidden"] alone', async () => {
    // Routed by URL: the section click also PUTs the global prefs.
    apiPut.mockImplementation((url: string) => Promise.resolve(
      url === '/admin/models/openai/luna' ? { id: 'openai/luna', subjects: ['hidden'] } : {},
    ));
    await openModels();
    await act(async () => { stop(modelRow('openai/luna'), 'adminModelHidden').click(); });
    expect(apiPut).toHaveBeenCalledWith('/admin/models/openai/luna', { subjects: ['hidden'] });
    expect(stop(modelRow('openai/luna'), 'adminModelHidden').getAttribute('aria-checked')).toBe('true');
  });

  it('the default model\'s Hidden stop is disabled', async () => {
    await openModels();
    expect(stop(modelRow('deepseek/flash'), 'adminModelHidden').disabled).toBe(true);
    expect(stop(modelRow('openai/luna'), 'adminModelHidden').disabled).toBe(false);
  });
});

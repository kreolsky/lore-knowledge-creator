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
  { project_id: 'p1', name: 'Alpha Project', owner_id: 'u1', owner_name: 'Alice Admin', is_public: false, my_access: 'full' },
  { project_id: 'p2', name: 'Beta Project', owner_id: 'u2', owner_name: 'Bob', is_public: true, my_access: 'readonly' },
  { project_id: 'p_locked', name: 'Locked Project', owner_id: 'u2', owner_name: 'Bob', is_public: false, my_access: null },
  ...Array.from({ length: 148 }, (_, i) => ({
    project_id: `p${i + 3}`,
    name: `Project ${String(i + 3).padStart(3, '0')}`,
    owner_id: 'u1',
    owner_name: 'Alice Admin',
    is_public: false,
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
  privateLabel: 'Private',
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

/** dsh pdf-body.client.spec.tsx pattern (first-party code has no other IO
 * stub; the component guards IntersectionObserver's absence in jsdom). */
class IntersectionObserverStub {
  static instances: IntersectionObserverStub[] = [];
  readonly observed = new Set<Element>();
  disconnected = false;

  constructor(private readonly callback: IntersectionObserverCallback) {
    IntersectionObserverStub.instances.push(this);
  }

  observe(element: Element): void { this.observed.add(element); }
  unobserve(element: Element): void { this.observed.delete(element); }
  disconnect(): void { this.disconnected = true; this.observed.clear(); }
  takeRecords(): IntersectionObserverEntry[] { return []; }
  intersect(element: Element, isIntersecting: boolean): void {
    this.callback([{ target: element, isIntersecting } as IntersectionObserverEntry], this as unknown as IntersectionObserver);
  }
}

beforeEach(async () => {
  vi.resetModules();
  appState = { currentUser: { user_id: 'u1', name: 'Alice Admin', role: 'admin', is_admin: true, can_manage_users: true }, showToast: vi.fn(), sectionOrigin: null, sectionCrumb: null };
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

function pressEscape(input: HTMLInputElement) {
  act(() => { input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); });
}

/** Outlive the 200 ms filter debounce (real timers; 250 ms sleep inside act). */
async function settleDebounce() {
  await act(async () => { await new Promise(r => setTimeout(r, 250)); });
}

function filterInput(placeholder: string): HTMLInputElement {
  const el = main()?.querySelector(`input[placeholder="${placeholder}"]`);
  expect(el, `filter input "${placeholder}"`).toBeDefined();
  return el as HTMLInputElement;
}

/** Dropdown triggers carry `title` (Role / group); the listbox mounts only
 * while open, so read options by clicking the trigger first. */
function dropdownTriggers(scope: ParentNode, title: string): HTMLButtonElement[] {
  return Array.from(scope.querySelectorAll(`button[title="${title}"]`));
}

async function dropdownOptionLabels(scope: ParentNode, title: string): Promise<string[]> {
  const [trigger] = dropdownTriggers(scope, title);
  expect(trigger, `dropdown "${title}"`).toBeDefined();
  await act(async () => { trigger.click(); });
  const labels = Array.from(scope.querySelectorAll('[role=option]')).map(o => o.textContent ?? '');
  await act(async () => { trigger.click(); });
  return labels;
}

async function pickDropdownOption(scope: ParentNode, title: string, label: string) {
  const [trigger] = dropdownTriggers(scope, title);
  expect(trigger, `dropdown "${title}"`).toBeDefined();
  await act(async () => { trigger.click(); });
  const opt = Array.from(scope.querySelectorAll('[role=option]')).find(o => o.textContent === label);
  expect(opt, `option "${label}" in "${title}"`).toBeDefined();
  await act(async () => { (opt as HTMLElement).click(); });
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
  it('aside lists all section rows (admin: Info on top, Groups + Model access under Users, settings tabs + Skills) with Users active; no top tabs; projects not fetched until opened', async () => {
    await render();
    const rows = Array.from(aside()!.querySelectorAll('button.doc-item'));
    expect(rows.map(r => r.textContent)).toEqual([
      'Info', 'Users', 'Groups', 'Model access', 'Projects',
      'Models & APIs', 'Search', 'Agent', 'Tools', 'Storage & Jobs', 'Embeddings', 'Skills',
    ]);
    expect(rows[1].classList.contains('active')).toBe(true);
    expect(container.querySelector('.left-bar-tab')).toBeNull();
    expect(projectsCalls()).toEqual([]);
    expect(main()?.textContent).toContain('Add user');
    // Sections are rows, not entities: no project row in the aside.
    expect(aside()?.textContent).not.toContain('Alpha Project');
  });

  it('a settings row swaps the center to that settings tab; the Skills row to the skills tab', async () => {
    await render();
    // By textContent, not the title attribute: jsdom's selector engine fails
    // to match an `&` inside an attribute value ("Storage & Jobs").
    const clickRow = (label: string) => {
      const btn = Array.from(aside()!.querySelectorAll<HTMLButtonElement>('button.doc-item'))
        .find(b => b.textContent === label);
      expect(btn, `section row "${label}"`).toBeDefined();
      act(() => btn!.click());
    };
    clickRow('Agent');
    expect(main()?.querySelector('[data-testid="settings-section-agent"]')).not.toBeNull();
    expect(main()?.textContent).not.toContain('Alpha Project');
    clickRow('Storage & Jobs');
    expect(main()?.querySelector('[data-testid="settings-section-storage"]')).not.toBeNull();
    clickRow('Skills');
    expect(main()?.querySelector('[data-testid="skills-section"]')).not.toBeNull();
    expect(main()?.querySelector('[data-testid^="settings-section-"]')).toBeNull();
  });

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
    // Access (owner, member, or public-readonly) → the link stays.
    expect(card('Alpha Project').querySelector('a[href="/projects/p1"]')).not.toBeNull();
    expect(card('Beta Project').querySelector('a[href="/projects/p2"]')).not.toBeNull();
  });

  it('the public/private toggle wears the accent fill only while public', async () => {
    await openProjects();
    const labelBtn = (title: string, label: string) =>
      Array.from(
        Array.from(main()!.querySelectorAll('section'))
          .find(s => s.textContent!.startsWith(title))!
          .querySelectorAll('button'),
      ).find(b => b.textContent === label)!;
    // Public state must read at a glance (accent = purple) that the project is
    // open to everyone; private stays the quiet ghost.
    expect(labelBtn('Beta Project', 'Public').classList.contains('bg-accent')).toBe(true);
    expect(labelBtn('Alpha Project', 'Private').classList.contains('bg-accent')).toBe(false);
  });
});

describe('AdminPage — ownership transfer (pinned owner row + no-jump swap)', () => {
  /** Alpha Project (p1): owner u1 (Alice), members Carol (commentator) and
   * Bob (readonly) — sort order [Carol, Bob], so Bob is the second member
   * row and the pick target. */
  async function openTransferProject(members: Record<string, string>) {
    (apiGet as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      const [path] = url.split('?');
      if (path === '/admin/users') return Promise.resolve(USERS);
      if (path === '/admin/projects') return Promise.resolve([ALL_PROJECTS[0]]);
      if (path === '/admin/projects/p1/members') return Promise.resolve(members);
      return Promise.resolve([]);
    });
    await openProjects();
    const card = main()!.querySelector('section')!;
    act(() => { card.querySelector<HTMLButtonElement>('button')!.click(); });
    await act(async () => {});
    return card;
  }

  /** Pinned owner row + member rows (the add-user picker row has no
   * items-center/gap-2 classes and is excluded). */
  function memberRows(card: HTMLElement): HTMLElement[] {
    return Array.from(card.querySelectorAll('div.flex.flex-col.gap-1 > div.flex.items-center.gap-2'));
  }

  it('pinned owner row renders first: disabled Owner dropdown, no remove button; members keep theirs', async () => {
    const card = await openTransferProject({ u2: 'readonly', u3: 'commentator' });
    const rows = memberRows(card);
    expect(rows).toHaveLength(3);
    const [ownerRow, carolRow, bobRow] = rows;
    expect(ownerRow.textContent).toContain('Alice Admin');
    expect(ownerRow.textContent).toContain('alice@x.test');
    const ownerTrigger = ownerRow.querySelector<HTMLButtonElement>('button[title="Owner"]');
    expect(ownerTrigger).not.toBeNull();
    expect(ownerTrigger!.disabled).toBe(true);
    expect(ownerRow.querySelector('button[title="Remove access"]')).toBeNull();
    for (const row of [carolRow, bobRow]) {
      expect(row.querySelector('button[title="Remove access"]')).not.toBeNull();
    }
    // Option labels come from the i18n fallback (raw keys) except ownerRole.
    expect(await dropdownOptionLabels(card, 'userAccess')).toEqual([
      'readOnly', 'commentator', 'fullAccess', 'Owner',
    ]);
  });

  it('picking Owner POSTs the transfer and swaps in place: old owner takes the slot, pinned row shows the new owner — no gap while the members reload', async () => {
    let transferred = false;
    (apiPost as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      if (url === '/admin/projects/p1/owner') {
        transferred = true;
        return Promise.resolve({ owner_id: 'u2', owner_name: 'Bob' });
      }
      return Promise.resolve({});
    });
    const card = await openTransferProject({ u2: 'readonly', u3: 'commentator' });
    // The members GET re-issued after the POST is held open, so the window
    // between the POST and the reload can be observed.
    let releaseMembers: (m: Record<string, string>) => void = () => {};
    (apiGet as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      const [path] = url.split('?');
      if (path === '/admin/users') return Promise.resolve(USERS);
      if (path === '/admin/projects') return Promise.resolve([ALL_PROJECTS[0]]);
      if (path === '/admin/projects/p1/members' && transferred) {
        return new Promise(resolve => { releaseMembers = resolve; });
      }
      return Promise.resolve([]);
    });

    const before = memberRows(card);
    expect(before).toHaveLength(3);
    expect(before[0].textContent).toContain('Alice Admin');
    // Pick Owner on Bob (the SECOND member row — readonly sorts last).
    const triggers = dropdownTriggers(card, 'userAccess');
    expect(triggers).toHaveLength(2);
    await act(async () => { triggers[1].click(); });
    const opt = Array.from(card.querySelectorAll('[role=option]')).find(o => o.textContent === 'Owner');
    expect(opt, 'Owner option').toBeDefined();
    await act(async () => { (opt as HTMLElement).click(); });
    await act(async () => {});

    expect(apiPost).toHaveBeenCalledWith('/admin/projects/p1/owner', { user_id: 'u2' });
    // POST done, members still loading: no row has vanished.
    const during = memberRows(card);
    expect(during).toHaveLength(3);
    expect(during[0].textContent).toContain('Alice Admin');

    await act(async () => { releaseMembers({ u1: 'full', u3: 'commentator' }); });
    await act(async () => {});
    const after = memberRows(card);
    expect(after).toHaveLength(3);
    // Pinned row = the new owner; Bob's former slot = Alice with Full.
    expect(after[0].textContent).toContain('Bob');
    expect(after[2].textContent).toContain('Alice Admin');
    expect(after[2].textContent).toContain('fullAccess');
  });

  it('a refused transfer toasts and leaves the rows unchanged', async () => {
    (apiPost as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('boom'));
    const card = await openTransferProject({ u2: 'readonly', u3: 'commentator' });
    const before = memberRows(card).map(r => r.textContent);
    const triggers = dropdownTriggers(card, 'userAccess');
    await act(async () => { triggers[1].click(); });
    const opt = Array.from(card.querySelectorAll('[role=option]')).find(o => o.textContent === 'Owner');
    await act(async () => { (opt as HTMLElement).click(); });
    await act(async () => {});
    await act(async () => {});
    expect(appState.showToast).toHaveBeenCalledWith('Failed to update access', 'error');
    const after = memberRows(card).map(r => r.textContent);
    expect(after).toEqual(before);
  });
});

describe('AdminPage — Users section', () => {
  async function openUsers() {
    await render();
    clickSection('Users');
    await act(async () => {});
  }

  it('center = add-user form, "Users" heading, filter, full user card list (with emails)', async () => {
    await openUsers();
    const text = main()?.textContent ?? '';
    expect(text).toContain('Add user');
    expect(text).toContain('Users');
    expect(filterInput('Filter users')).toBeDefined();
    expect(text).toContain('bob@x.test');
    expect(text).toContain('alice@x.test');
    expect(text).not.toContain('Alpha Project');
    expect(aside()?.querySelector('button.doc-item.active')?.textContent).toBe('Users');
  });

  it('Embeddings row swaps the center to the embeddings tab', async () => {
    await render();
    clickSection('Embeddings');
    expect(main()?.querySelector('[data-testid="embeddings-tab"]')).not.toBeNull();
    expect(main()?.textContent).not.toContain('Alpha Project');
  });

  it('header crumb follows the ACTIVE section and clears on unmount', async () => {
    await render();
    expect(appState.sectionCrumb).toBe('Users');
    clickSection('Embeddings');
    expect(appState.sectionCrumb).toBe('Embeddings');
    act(() => root.unmount());
    expect(appState.sectionCrumb).toBeNull();
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

  it('saved section unavailable to this user (moderator, embeddings) → Users', async () => {
    appState.currentUser = { user_id: 'm1', name: 'Mia Mod', role: 'moderator', can_manage_users: true, is_admin: false };
    useUIStore.setState({ adminSectionTab: 'embeddings' });
    await render();
    expect(aside()?.querySelector('button.doc-item.active')?.textContent).toBe('Users');
    expect(main()?.querySelector('[data-testid="embeddings-tab"]')).toBeNull();
    expect(main()?.textContent).toContain('Add user');
  });

  it('saved settings/skills section unavailable to a moderator → Users, no settings/skills fetch', async () => {
    appState.currentUser = { user_id: 'm1', name: 'Mia Mod', role: 'moderator', can_manage_users: true, is_admin: false };
    for (const saved of ['settings:agent', 'skills'] as const) {
      useUIStore.setState({ adminSectionTab: saved });
      await render();
      expect(aside()?.querySelector('button.doc-item.active')?.textContent).toBe('Users');
      expect(aside()?.textContent).not.toContain('Skills');
      expect(aside()?.textContent).not.toContain('Model access');
      expect(aside()?.textContent).not.toContain('Groups');
      expect(main()?.querySelector('[data-testid^="settings-section-"]')).toBeNull();
      expect(main()?.querySelector('[data-testid="skills-section"]')).toBeNull();
      expect(main()?.textContent).toContain('Add user');
      // The sections are mounted only inside the admin-only components — a
      // moderator's admin-page open must fire no /admin/settings|skills read.
      expect(apiGet.mock.calls.map(c => c[0] as string)
        .filter(u => u.startsWith('/admin/settings') || u.startsWith('/admin/skills'))).toEqual([]);
      // Remount for the next saved value: the initial tab is a mount-time
      // snapshot, later store changes do not yank the open section.
      act(() => { root.unmount(); });
      container = document.createElement('div');
      document.body.appendChild(container);
      root = createRoot(container);
    }
  });

  it('saved garbage section string → Users (availability check, not a crash)', async () => {
    useUIStore.setState({ adminSectionTab: 'nope' as unknown as 'embeddings' });
    await render();
    expect(aside()?.querySelector('button.doc-item.active')?.textContent).toBe('Users');
  });

  it('clicking a section row PUTs _global with the chosen tab', async () => {
    await render();
    clickSection('Projects');
    await act(async () => {});
    expect(lastGlobalPrefs().adminSectionTab).toBe('projects');
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

describe('AdminPage — projects filter (server-side q, debounced)', () => {
  it('typing replaces the list via /admin/projects?q=…&offset=0', async () => {
    await openProjects();
    await typeInto(filterInput('Filter projects'), 'Alpha');
    await settleDebounce();
    expect(projectsCalls().at(-1)).toBe('/admin/projects?q=Alpha&offset=0&limit=100');
    expect(main()?.textContent).toContain('Alpha Project');
    expect(main()?.textContent).not.toContain('Beta Project');
  });

  it('Escape clears + blurs the input and re-requests with empty q', async () => {
    await openProjects();
    const input = filterInput('Filter projects');
    await typeInto(input, 'Alpha');
    await settleDebounce();
    act(() => { input.focus(); });
    pressEscape(input);
    expect(input.value).toBe('');
    expect(document.activeElement).not.toBe(input);
    await settleDebounce();
    expect(projectsCalls().at(-1)).toBe('/admin/projects?offset=0&limit=100');
    expect(main()?.textContent).toContain('Beta Project');
  });
});

describe('AdminPage — projects paging by scroll (sentinel)', () => {
  it('a sentinel intersection requests offset=100 and appends the page; an exhausted list does not re-request', async () => {
    IntersectionObserverStub.instances.length = 0;
    vi.stubGlobal('IntersectionObserver', IntersectionObserverStub);
    await openProjects();
    const io = IntersectionObserverStub.instances.at(-1);
    expect(io, 'sentinel observer').toBeDefined();
    const sentinel = [...io!.observed][0];
    expect(sentinel, 'observed sentinel element').toBeDefined();
    expect(main()?.textContent).not.toContain('Project 150');
    // The sentinel sits at the END of the center list.
    expect(main()?.contains(sentinel!)).toBe(true);
    await act(async () => { io!.intersect(sentinel!, true); });
    await act(async () => {});
    expect(projectsCalls().at(-1)).toBe('/admin/projects?offset=100&limit=100');
    expect(main()?.textContent).toContain('Project 150');
    // Page 2 returned 50 < 100 → list exhausted: a repeat intersection is a no-op.
    await act(async () => { io!.intersect(sentinel!, true); });
    await act(async () => {});
    expect(projectsCalls().filter(u => u.includes('offset=100'))).toHaveLength(1);
  });
});

describe('AdminPage — users filter (client-side, name OR email)', () => {
  it('fetches users once on mount (limit=1000) and filters by name', async () => {
    await render();
    expect(apiGet.mock.calls.map(c => c[0] as string)).toContain('/admin/users?limit=1000');
    clickSection('Users');
    await act(async () => {});
    // "Adm" is in Alice's NAME only — no email contains it.
    await typeInto(filterInput('Filter users'), 'Adm');
    expect(main()?.textContent).toContain('Alice Admin');
    expect(main()?.textContent).not.toContain('Bob');
  });

  it('matches by email as well as name', async () => {
    await render();
    clickSection('Users');
    await act(async () => {});
    // "alice@x" is in Alice's EMAIL only — neither name contains it.
    await typeInto(filterInput('Filter users'), 'alice@x');
    expect(main()?.textContent).toContain('Alice Admin');
    expect(main()?.textContent).not.toContain('Bob');
  });
});

describe('AdminPage — user card: collapsed row, expand-down edit panel, one PATCH', () => {
  /** The card <section> whose text carries the email. */
  function card(email: string): HTMLElement {
    const el = Array.from(main()?.querySelectorAll('section') ?? [])
      .find(sec => sec.textContent?.includes(email));
    expect(el, `card for ${email}`).toBeDefined();
    return el!;
  }
  function inputs(sec: HTMLElement): HTMLInputElement[] {
    return Array.from(sec.querySelectorAll('input'));
  }
  function clickPencil(email: string) {
    act(() => { (card(email).querySelector('button[title="Edit name / email"]') as HTMLButtonElement).click(); });
  }
  async function openUsers() {
    await render();
    clickSection('Users');
    await act(async () => {});
  }

  it('collapsed row = name · email | role label, one pencil, no inputs', async () => {
    await openUsers();
    const bob = card('bob@x.test');
    expect(bob.textContent).toContain('Bob');
    expect(bob.textContent).toContain('| User');
    // A grouped user shows `group: <name>` on the SAME line, labelled —
    // never a second row.
    const carol = card('carol@x.test');
    expect(Array.from(carol.querySelectorAll('span.whitespace-nowrap')).map(el => el.textContent)).toEqual(['| User', '| group: Bob', '| invited by: Alice Admin']);
    expect(carol.querySelectorAll('div').length).toBe(1);
    expect(bob.querySelectorAll('button[title]')).toHaveLength(1);
    expect(bob.querySelector('button[title]')?.getAttribute('title')).toBe('Edit name / email');
    expect(inputs(bob)).toHaveLength(0);
  });

  it('pencil expands a panel with prefilled name/email, empty password, Save/Cancel, delete; another pencil closes it; self has no delete', async () => {
    await openUsers();
    clickPencil('bob@x.test');
    const bob = card('bob@x.test');
    expect(inputs(bob).map(i => i.value)).toEqual(['Bob', 'bob@x.test', '']);
    expect(inputs(bob)[2].type).toBe('password');
    expect(bob.textContent).toContain('Save');
    expect(bob.textContent).toContain('Cancel');
    expect(bob.querySelector('button[title="Delete user"]')).not.toBeNull();

    clickPencil('alice@x.test');
    expect(inputs(card('bob@x.test'))).toHaveLength(0);
    const alice = card('alice@x.test');
    expect(inputs(alice)).toHaveLength(3);
    // Alice is the current user (u1) — no self-delete.
    expect(alice.querySelector('button[title="Delete user"]')).toBeNull();
  });

  it('Save sends ONE PATCH with only the changed name + the typed password (no email key) and closes the panel', async () => {
    await openUsers();
    apiPatch.mockResolvedValueOnce({ user_id: 'u2', name: 'Bobby', email: 'bob@x.test', role: 'user', has_pin: false });
    clickPencil('bob@x.test');
    const [nameInput, , pwdInput] = inputs(card('bob@x.test'));
    await typeInto(nameInput, 'Bobby');
    await typeInto(pwdInput, 's3cret');
    clickButtonWithText(card('bob@x.test'), 'Save');
    await act(async () => {});
    expect(apiPatch.mock.calls).toEqual([['/admin/users/u2', { name: 'Bobby', password: 's3cret' }]]);
    expect(inputs(card('bob@x.test'))).toHaveLength(0);
    expect(card('bob@x.test').textContent).toContain('Bobby');
  });

  it('Save with nothing changed closes without a request; Cancel closes without a request', async () => {
    await openUsers();
    clickPencil('bob@x.test');
    clickButtonWithText(card('bob@x.test'), 'Save');
    await act(async () => {});
    expect(apiPatch).not.toHaveBeenCalled();
    expect(inputs(card('bob@x.test'))).toHaveLength(0);

    clickPencil('bob@x.test');
    await typeInto(inputs(card('bob@x.test'))[0], 'Changed');
    clickButtonWithText(card('bob@x.test'), 'Cancel');
    expect(apiPatch).not.toHaveBeenCalled();
    expect(inputs(card('bob@x.test'))).toHaveLength(0);
  });

  it('Escape in the name input closes the panel without a request', async () => {
    await openUsers();
    clickPencil('bob@x.test');
    pressEscape(inputs(card('bob@x.test'))[0]);
    expect(apiPatch).not.toHaveBeenCalled();
    expect(inputs(card('bob@x.test'))).toHaveLength(0);
  });
});

describe('AdminPage — moderator view (can_manage_users && !is_admin)', () => {
  function localCard(email: string): HTMLElement {
    const el = Array.from(main()?.querySelectorAll('section') ?? [])
      .find(sec => sec.textContent?.includes(email));
    expect(el, `card for ${email}`).toBeDefined();
    return el!;
  }

  async function renderAsModerator() {
    appState.currentUser = { user_id: 'm1', name: 'Mia Mod', role: 'moderator', can_manage_users: true, is_admin: false };
    await render();
    await act(async () => {});
  }

  it('stays on the page (no redirect) and the aside lists ONLY Projects + Users', async () => {
    await renderAsModerator();
    expect(navigate).not.toHaveBeenCalledWith('/');
    const rows = Array.from(aside()!.querySelectorAll('button.doc-item'));
    expect(rows.map(r => r.textContent)).toEqual(['Users', 'Projects']);
  });

  it('Users section: add form has no role/moderator selects; invite button POSTs /api/invites and copies the /register URL built from the page origin', async () => {
    await renderAsModerator();
    clickSection('Users');
    await act(async () => {});
    const addForm = main()!.querySelector('section')!;
    expect(dropdownTriggers(addForm, 'Role')).toHaveLength(0);
    expect(dropdownTriggers(addForm, 'group')).toHaveLength(0);

    const clip = vi.fn(() => Promise.resolve());
    Object.assign(navigator, { clipboard: { writeText: clip } });
    (apiPost as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ token: 'tok1', expires_at: '2026-09-22T00:00:00Z' });
    const inviteBtn = Array.from(main()!.querySelectorAll('button'))
      .find(b => b.textContent?.includes('Invite link'));
    expect(inviteBtn, 'invite link button').toBeDefined();
    await act(async () => { inviteBtn!.click(); });
    expect(apiPost).toHaveBeenCalledWith('/invites', {});
    // The URL is composed client-side: page origin (read off the live
    // window.location.origin — never a hard-coded jsdom origin) + /register/.
    expect(clip).toHaveBeenCalledWith(`${window.location.origin}/register/tok1`);
    expect(appState.showToast).toHaveBeenCalledWith('Invite link copied', 'info');
  });

  it('Projects section: expanded project offers the add-member dropdown and member remove buttons', async () => {
    (apiGet as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      const [path] = url.split('?');
      if (path === '/admin/users') return Promise.resolve(USERS);
      if (path === '/admin/projects') return Promise.resolve([ALL_PROJECTS[1]]);
      if (path === '/admin/projects/p2/members') return Promise.resolve({ u3: 'readonly' });
      return Promise.resolve([]);
    });
    await renderAsModerator();
    clickSection('Projects');
    await act(async () => {});
    const card = main()!.querySelector('section')!;
    act(() => { card.querySelector<HTMLButtonElement>('button')!.click(); });
    await act(async () => {});
    expect(dropdownTriggers(card, 'addUserPlaceholder')).toHaveLength(1);
    expect(dropdownTriggers(card, 'userAccess')).toHaveLength(1);
    expect(card.querySelector('button[title="Remove access"]')).not.toBeNull();
    // The scoped user list excludes the moderator; they may still add THEMSELF
    // (self is in the backend scope set) — so self heads the candidate list.
    expect(await dropdownOptionLabels(card, 'addUserPlaceholder')).toEqual(['Mia Mod', 'Alice Admin']);
  });

  it('removing MYSELF from a project disables its open button at once', async () => {
    const project = {
      project_id: 'p9', name: 'Group Project', owner_id: 'u3', owner_name: 'Carol',
      is_public: false, my_access: 'commentator',
    };
    (apiGet as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      const [path] = url.split('?');
      if (path === '/admin/users') return Promise.resolve(USERS);
      if (path === '/admin/projects') return Promise.resolve([project]);
      if (path === '/admin/projects/p9/members') return Promise.resolve({ m1: 'commentator' });
      return Promise.resolve([]);
    });
    await renderAsModerator();
    clickSection('Projects');
    await act(async () => {});
    const card = main()!.querySelector('section')!;
    // Member (commentator) → the open link is live.
    expect(card.querySelector('a[href="/projects/p9"]')).not.toBeNull();
    act(() => { card.querySelector<HTMLButtonElement>('button')!.click(); });
    await act(async () => {});
    await act(async () => { (card.querySelector('button[title="Remove access"]') as HTMLButtonElement).click(); });
    expect(apiDelete).toHaveBeenCalledWith('/admin/projects/p9/members/m1');
    // Private, not a member anymore → disabled immediately, no reload.
    expect(card.querySelector('a[href="/projects/p9"]')).toBeNull();
    expect(card.querySelector('span[title="No access"]')).not.toBeNull();
  });

  it('user edit panel has no role select for a moderator', async () => {
    await renderAsModerator();
    clickSection('Users');
    await act(async () => {});
    act(() => { (localCard('bob@x.test').querySelector('button[title="Edit name / email"]') as HTMLButtonElement).click(); });
    await act(async () => {});
    expect(dropdownTriggers(localCard('bob@x.test'), 'Role')).toHaveLength(0);
    expect(dropdownTriggers(localCard('bob@x.test'), 'group')).toHaveLength(0);
  });
});

describe('AdminPage — admin-only surfaces', () => {
  function localCard(email: string): HTMLElement {
    const el = Array.from(main()?.querySelectorAll('section') ?? [])
      .find(sec => sec.textContent?.includes(email));
    expect(el, `card for ${email}`).toBeDefined();
    return el!;
  }

  it('add form carries the role select (Moderator option) and the moderator select for role=user', async () => {
    await render();
    clickSection('Users');
    await act(async () => {});
    const addForm = main()!.querySelector('section')!;
    expect(await dropdownOptionLabels(addForm, 'Role')).toEqual(['User', 'Moderator', 'Admin']);
    expect(dropdownTriggers(addForm, 'group')).toHaveLength(1);
    // Opening a selector inside the <form> must not submit it (a default
    // type=submit trigger POSTed an empty user → "Failed to create user").
    await pickDropdownOption(addForm, 'Role', 'Moderator');
    expect(apiPost).not.toHaveBeenCalled();
    expect(dropdownTriggers(addForm, 'group')).toHaveLength(0);
  });

  it('edit panel: generate fills + copies a password, clear empties it, Save sends it', async () => {
    const clip = vi.fn(() => Promise.resolve());
    Object.assign(navigator, { clipboard: { writeText: clip } });
    await render();
    clickSection('Users');
    await act(async () => {});
    act(() => { (localCard('bob@x.test').querySelector('button[title="Edit name / email"]') as HTMLButtonElement).click(); });
    await act(async () => {});
    const panel = localCard('bob@x.test').querySelector('div.border-t')!;
    const pwd = () => panel.querySelector('input[type=password]') as HTMLInputElement;
    const clear = () => panel.querySelector('button[title="Clear password"]') as HTMLButtonElement;
    expect(clear().disabled).toBe(true);
    act(() => { (panel.querySelector('button[title="Generate password"]') as HTMLButtonElement).click(); });
    expect(pwd().value).toHaveLength(12);
    expect(clip).toHaveBeenCalledWith(pwd().value);
    expect(appState.showToast).toHaveBeenCalledWith('Copied!', 'info');
    expect(clear().disabled).toBe(false);
    act(() => { clear().click(); });
    expect(pwd().value).toBe('');
    // Neither button submitted anything.
    expect(apiPatch).not.toHaveBeenCalled();
  });

  it('a refused create toasts the server reason and keeps the draft', async () => {
    const { HttpError } = await vi.importActual<typeof import('../api/client')>('../api/client');
    await render();
    clickSection('Users');
    await act(async () => {});
    const addForm = main()!.querySelector('section')!;
    const [name, email, password] = Array.from(addForm.querySelectorAll('input'));
    await typeInto(name, 'Dave');
    await typeInto(email, 'dave@x.test');
    await typeInto(password, 'hunter22');
    (apiPost as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new HttpError(409, '{"detail":"Email already in use"}'));
    clickButtonWithText(addForm, 'Add');
    await act(async () => {});
    expect(apiPost).toHaveBeenCalledWith('/admin/users', { name: 'Dave', email: 'dave@x.test', password: 'hunter22', role: 'user', moderator_id: null });
    expect(appState.showToast).toHaveBeenCalledWith('Email already in use', 'error');
    expect(Array.from(addForm.querySelectorAll('input')).map(i => i.value)).toEqual(['Dave', 'dave@x.test', 'hunter22']);
  });

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

  it('a user promoted to moderator is offered in the group dropdown at once, without a reload', async () => {
    await render();
    clickSection('Users');
    await act(async () => {});
    act(() => { (localCard('bob@x.test').querySelector('button[title="Edit name / email"]') as HTMLButtonElement).click(); });
    await act(async () => {});
    (apiPatch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ ...USERS[1], role: 'moderator' });
    await pickDropdownOption(localCard('bob@x.test'), 'Role', 'Moderator');
    clickButtonWithText(localCard('bob@x.test'), 'Save');
    await act(async () => {});
    expect(apiPatch.mock.calls).toEqual([['/admin/users/u2', { role: 'moderator' }]]);

    act(() => { (localCard('carol@x.test').querySelector('button[title="Edit name / email"]') as HTMLButtonElement).click(); });
    await act(async () => {});
    expect(await dropdownOptionLabels(localCard('carol@x.test'), 'group')).toEqual(['No group', 'Bob']);
    // The add form's group dropdown reads the same derived list.
    expect(await dropdownOptionLabels(main()!.querySelector('section')!, 'group')).toEqual(['No group', 'Bob']);
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

    // pydantic 422 shape: detail is an ARRAY — the short-password refusal.
    // (The panel closes after every Save — reopen it for the second round.)
    (apiPatch as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new HttpError(422, '{"detail":[{"type":"string_too_short","loc":["body","password"],"msg":"String should have at least 6 characters"}]}'));
    act(() => { (localCard('bob@x.test').querySelector('button[title="Edit name / email"]') as HTMLButtonElement).click(); });
    await act(async () => {});
    const reopened = localCard('bob@x.test').querySelectorAll('input')[0];
    await typeInto(reopened, 'Bobby');
    clickButtonWithText(localCard('bob@x.test'), 'Save');
    await act(async () => {});
    expect(appState.showToast).toHaveBeenCalledWith('String should have at least 6 characters');
  });
});

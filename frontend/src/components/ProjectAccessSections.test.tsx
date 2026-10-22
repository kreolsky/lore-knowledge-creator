/**
 * ProjectAccessSections — the sharing gates of the settings panel.
 *
 * The unified audiences surface (plan unified-project-audience): the admin
 * edits the FULL multi-row list (subject + level + ×, one trailing empty row),
 * a group-bound owner keeps the 4-stop own-group control while every foreign
 * row (incl. "All users") is SHOWN as a note with ×, and a no-group owner sees
 * the note list only. A non-admin never gets "All users" as an option; every
 * change PATCHes the whole list, so a failed PATCH leaves the rows at the
 * stored list (the panel's catch toasts; the section is fully controlled).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, act, type ReactElement, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let Sections: typeof import('./ProjectAccessSections');
let apiAccess: typeof import('../api/access');
let apiAdmin: typeof import('../api/admin');
/** apiClient.patch mock — the ProjectSettingsPanel failure path drives it. */
let patchMock: ReturnType<typeof vi.fn>;
let showToast: ReturnType<typeof vi.fn>;
/** Latest FieldInput props (the invite filter field). */
let fieldInputProps: { value: string; onChange: (e: { target: { value: string } }) => void } | null = null;
/** All Dropdown renders of the last mount, in render order. */
interface DropdownStubProps {
  value: string;
  options: { value: string; label: string; note?: string; disabled?: boolean }[];
  onSelect: (v: string) => void;
  disabled?: boolean;
  triggerLabel?: string;
  filterPlaceholder?: string;
  align?: string;
}
let dropdownProps: DropdownStubProps[] = [];
/** Latest SegmentedControl props. */
interface SegmentedStubProps {
  value: string;
  options: { value: string; label: string; disabled?: boolean }[];
  onChange: (v: string) => void;
  disabled?: boolean;
}
let segmentedProps: SegmentedStubProps | null = null;
let appState: Record<string, unknown>;

import type { Audience, Project } from '../types';

const OWNER = { user_id: 'u1', name: 'Owen', email: 'owen@x.test', role: 'user' as const, has_pin: false, user_facts: '' };
const ADMIN = { ...OWNER, user_id: 'a1', role: 'admin' as const, is_admin: true, can_manage_users: true, can_publish_project: true };

const row = (subject: string, access_level: Audience['access_level']): Audience => ({ subject, access_level });

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    project_id: 'p1',
    name: 'P',
    status: 'active',
    project_context: '',
    index_doc_id: null,
    voice_recording_doc_id: null,
    last_accessed_doc_id: null,
    owner_id: 'u1',
    audiences: [],
    my_access: 'full',
    is_owner_like: true,
    created_at: '2026-01-01',
    ...overrides,
  };
}

/** The admin-groups payload shape (GET /api/admin/groups) the options come from. */
function makeGroups() {
  return [
    { id: 'g1', subject: 'group:g1', kind: 'admin' as const, name: 'Editors', members: [], models: [] },
    { id: 'm1', subject: 'mod:m1', kind: 'moderator' as const, name: 'Mia Mod', members: [], models: [] },
  ];
}

beforeEach(async () => {
  vi.resetModules();
  fieldInputProps = null;
  dropdownProps = [];
  segmentedProps = null;
  showToast = vi.fn();
  // STABLE identities (module-level t; the stubs re-render per act) — a fresh
  // t per render would re-fire the roster/members effects (frontend.md).
  const tFn = (k: string) => k;

  vi.doMock('../i18n', () => ({ t: tFn, useTranslation: () => ({ t: tFn }) }));
  vi.doMock('../store/app-store', () => {
    const useAppStore = (sel: (s: Record<string, unknown>) => unknown) => sel(appState);
    (useAppStore as unknown as { getState: () => Record<string, unknown> }).getState = () => appState;
    return { useAppStore };
  });
  vi.doMock('./ui', () => ({
    Button: (p: { onClick?: () => void; disabled?: boolean; children: ReactNode }) =>
      createElement('button', { type: 'button', onClick: p.onClick, disabled: p.disabled }, p.children),
    IconButton: (p: { title?: string; onClick?: () => void; children: ReactNode; 'data-remove-subject'?: string }) =>
      createElement('button', { type: 'button', title: p.title, onClick: p.onClick, 'data-remove-subject': p['data-remove-subject'] }, p.children),
    FieldCheckbox: (p: { checked: boolean; onChange: (v: boolean) => void; label: string }) =>
      createElement('input', { type: 'checkbox', 'data-label': p.label, checked: p.checked, onChange: (e: unknown) => p.onChange((e as { target: { checked: boolean } }).target.checked) }),
    FieldRadio: (p: { checked: boolean; onChange: () => void; label?: string }) =>
      createElement('input', { type: 'radio', checked: p.checked, readOnly: true, onChange: p.onChange }),
    FieldInput: (p: { value: string; onChange: (e: { target: { value: string } }) => void; placeholder?: string }) => {
      fieldInputProps = p;
      return createElement('input', { type: 'email', value: p.value, placeholder: p.placeholder, onChange: () => {} });
    },
    SectionHeader: (p: { title: string }) => createElement('div', { 'data-section': p.title }, p.title),
    Dropdown: (p: DropdownStubProps) => {
      dropdownProps.push(p);
      return createElement('div', null, p.options.map(o =>
        createElement('button', { key: o.value, type: 'button', onClick: () => p.onSelect(o.value) }, o.label)));
    },
    SegmentedControl: (p: SegmentedStubProps) => {
      segmentedProps = p;
      return createElement('div', { role: 'radiogroup' }, p.options.map(o =>
        createElement('button', { key: o.value, type: 'button', onClick: () => p.onChange(o.value) }, o.label)));
    },
  }));
  const icon = () => null;
  vi.doMock('lucide-react', () => ({ X: icon, Users: icon, UsersRound: icon, Globe: icon, Mic: icon, ChevronRight: icon, Image: icon }));
  vi.doMock('./ParentPickerPopup', () => ({ ParentPickerPopup: () => null }));
  patchMock = vi.fn().mockResolvedValue({});
  vi.doMock('../api/client', () => ({ apiClient: { patch: patchMock } }));
  apiAccess = await (async () => {
    const mod = {
      fetchGroupRoster: vi.fn().mockResolvedValue([]),
      inviteMember: vi.fn().mockResolvedValue({ queued: true }),
      listMembers: vi.fn().mockResolvedValue([]),
      patchMember: vi.fn().mockResolvedValue(undefined),
      removeMember: vi.fn().mockResolvedValue(undefined),
      cancelInvite: vi.fn().mockResolvedValue(undefined),
    };
    vi.doMock('../api/access', () => mod);
    return mod as unknown as typeof import('../api/access');
  })();
  apiAdmin = await (async () => {
    const mod = { listAdminGroups: vi.fn().mockResolvedValue([]) };
    vi.doMock('../api/admin', () => mod);
    return mod as unknown as typeof import('../api/admin');
  })();

  ({ ...Sections } = await import('./ProjectAccessSections'));

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('../i18n');
  vi.doUnmock('../store/app-store');
  vi.doUnmock('./ui');
  vi.doUnmock('lucide-react');
  vi.doUnmock('./ParentPickerPopup');
  vi.doUnmock('../api/client');
  vi.doUnmock('../api/access');
  vi.doUnmock('../api/admin');
});

function setSession(user: Record<string, unknown>, project: Project | null) {
  appState = { currentUser: user, currentProject: project, showToast, documents: [], setCurrentProject: vi.fn() };
}

async function renderTree(el: ReactElement) {
  await act(async () => { root.render(el); });
  await act(async () => {});
}

function section(title: string) {
  return container.querySelector(`[data-section="${title}"]`);
}

function inviteButton() {
  const btn = Array.from(container.querySelectorAll('button'))
    .find(b => b.textContent === 'accessInvite');
  expect(btn, 'the Invite button').toBeDefined();
  return btn!;
}

/** The level Dropdowns (their options are access levels), in render order. */
function levelPickers() {
  return dropdownProps.filter(d =>
    d.options.length > 0 && d.options.every(o => ['readonly', 'commentator', 'full'].includes(o.value)));
}

describe('AudienceSection — admin: the multi-row editor', () => {
  async function renderAudience(project: Partial<Project>, onPatch: (u: Record<string, unknown>) => void) {
    await renderTree(createElement(Sections.AudienceSection, { project: makeProject(project), onPatch, busy: false }));
  }

  it('options come from ONE listAdminGroups call; the trailing empty row lists All users, admin groups, moderator groups', async () => {
    setSession({ ...ADMIN }, makeProject());
    (apiAdmin.listAdminGroups as ReturnType<typeof vi.fn>).mockResolvedValue(makeGroups());
    await renderAudience({}, vi.fn());
    expect(apiAdmin.listAdminGroups).toHaveBeenCalledTimes(1);
    const emptyRow = dropdownProps[dropdownProps.length - 1]!;
    expect(emptyRow.value).toBe('');
    expect(emptyRow.options.map(o => o.value)).toEqual(['', 'public', 'group:g1', 'mod:m1']);
    expect(emptyRow.options.map(o => o.label)).toEqual(['noGroup', 'accessAllUsers', 'Editors', 'Mia Mod']);
    expect(emptyRow.align).toBe('left');
  });

  it('choosing a subject in the empty row PATCHes the new row; a fresh empty row follows and used subjects are not offered twice', async () => {
    setSession({ ...ADMIN }, makeProject());
    (apiAdmin.listAdminGroups as ReturnType<typeof vi.fn>).mockResolvedValue(makeGroups());
    const onPatch = vi.fn();
    await renderAudience({}, onPatch);
    const emptyRow = dropdownProps[dropdownProps.length - 1]!;
    await act(async () => { emptyRow.onSelect('public'); });
    expect(onPatch).toHaveBeenCalledWith({ audiences: [row('public', 'readonly')] });

    await renderAudience({ audiences: [row('public', 'readonly')] }, onPatch);
    const nextEmpty = dropdownProps[dropdownProps.length - 1]!;
    expect(nextEmpty.value).toBe('');
    expect(nextEmpty.options.map(o => o.value)).toEqual(['', 'group:g1', 'mod:m1']);
  });

  it('a public row offers only readonly/commentator; a group row all three', async () => {
    setSession({ ...ADMIN }, makeProject());
    (apiAdmin.listAdminGroups as ReturnType<typeof vi.fn>).mockResolvedValue(makeGroups());
    await renderAudience({ audiences: [row('public', 'commentator'), row('mod:m1', 'full')] }, vi.fn());
    // The groups fetch re-renders the editor — dropdownProps records BOTH
    // passes, so read the LAST render's pickers (the final two).
    const levels = levelPickers().slice(-2);
    expect(levels.map(d => d.options.map(o => o.value))).toEqual([
      ['readonly', 'commentator'],
      ['readonly', 'commentator', 'full'],
    ]);
  });

  it('a level change PATCHes the list with that row replaced', async () => {
    setSession({ ...ADMIN }, makeProject());
    (apiAdmin.listAdminGroups as ReturnType<typeof vi.fn>).mockResolvedValue(makeGroups());
    const onPatch = vi.fn();
    await renderAudience({ audiences: [row('mod:m1', 'readonly')] }, onPatch);
    await act(async () => { levelPickers()[0]!.onSelect('full'); });
    expect(onPatch).toHaveBeenCalledWith({ audiences: [row('mod:m1', 'full')] });
  });

  it('re-pointing a filled row swaps the subject and clamps public full → readonly', async () => {
    setSession({ ...ADMIN }, makeProject());
    (apiAdmin.listAdminGroups as ReturnType<typeof vi.fn>).mockResolvedValue(makeGroups());
    const onPatch = vi.fn();
    await renderAudience({ audiences: [row('mod:m1', 'full')] }, onPatch);
    const subjectPicker = dropdownProps.find(d => d.value === 'mod:m1')!;
    await act(async () => { subjectPicker.onSelect('public'); });
    expect(onPatch).toHaveBeenCalledWith({ audiences: [row('public', 'readonly')] });
  });

  it('remove × PATCHes the list without the row', async () => {
    setSession({ ...ADMIN }, makeProject());
    (apiAdmin.listAdminGroups as ReturnType<typeof vi.fn>).mockResolvedValue(makeGroups());
    const onPatch = vi.fn();
    await renderAudience({ audiences: [row('public', 'commentator'), row('mod:m1', 'full')] }, onPatch);
    await act(async () => { (container.querySelector('[data-remove-subject="public"] button') as HTMLButtonElement).click(); });
    expect(onPatch).toHaveBeenCalledWith({ audiences: [row('mod:m1', 'full')] });
  });
});

describe('AudienceSection — group owner (non-admin)', () => {
  async function renderOwned(project: Partial<Project>, onPatch: (u: Record<string, unknown>) => void) {
    setSession({ ...OWNER, group_subject: 'mod:m1' }, makeProject(project));
    await renderTree(createElement(Sections.AudienceSection, { project: makeProject(project), onPatch, busy: false }));
  }

  it('the 4-stop control edits only the own-subject row; foreign rows ride along in the body', async () => {
    const onPatch = vi.fn();
    await renderOwned({ audiences: [row('mod:m1', 'readonly'), row('public', 'commentator')] }, onPatch);
    expect(segmentedProps!.options.map(o => o.value)).toEqual(['none', 'readonly', 'commentator', 'full']);
    expect(segmentedProps!.value).toBe('readonly');
    await act(async () => { segmentedProps!.onChange('full'); });
    expect(onPatch).toHaveBeenCalledWith({ audiences: [row('mod:m1', 'full'), row('public', 'commentator')] });
    await act(async () => { segmentedProps!.onChange('none'); });
    expect(onPatch).toHaveBeenCalledWith({ audiences: [row('public', 'commentator')] });
    // No own row yet → a level pick APPENDS it, foreign rows kept.
    await renderOwned({ audiences: [row('public', 'commentator')] }, onPatch);
    await act(async () => { segmentedProps!.onChange('commentator'); });
    expect(onPatch).toHaveBeenCalledWith({ audiences: [row('public', 'commentator'), row('mod:m1', 'commentator')] });
  });

  it('the control is NOT disabled while foreign rows exist (the bound-elsewhere lock is gone)', async () => {
    await renderOwned({ audiences: [row('mod:m1', 'readonly'), row('group:g9', 'full')] }, vi.fn());
    expect(segmentedProps!.disabled).toBe(false);
    expect(segmentedProps!.options.some(o => o.disabled)).toBe(false);
  });

  it('every foreign row (incl. All users) is a note with ×; × PATCHes the list without it', async () => {
    const onPatch = vi.fn();
    await renderOwned({ audiences: [row('mod:m1', 'readonly'), row('public', 'commentator'), row('group:g9', 'full')] }, onPatch);
    const notes = Array.from(container.querySelectorAll('[data-audience-note]'))
      .map(n => n.getAttribute('data-audience-note'));
    expect(notes).toEqual(['public', 'group:g9']);
    expect(container.querySelector('[data-audience-note="public"]')!.textContent).toContain('accessAllUsers');
    expect(container.querySelector('[data-audience-note="group:g9"]')!.textContent).toContain('group:g9');
    await act(async () => { (container.querySelector('[data-audience-note="public"] button') as HTMLButtonElement).click(); });
    expect(onPatch).toHaveBeenCalledWith({ audiences: [row('mod:m1', 'readonly'), row('group:g9', 'full')] });
  });

  it('a non-admin never sees "All users" as an option', async () => {
    await renderOwned({ audiences: [row('public', 'commentator')] }, vi.fn());
    expect(dropdownProps.some(d => d.options.some(o => o.value === 'public'))).toBe(false);
  });
});

describe('AudienceSection — owner without a group', () => {
  it('no rows → no section at all', async () => {
    setSession({ ...OWNER }, makeProject());
    await renderTree(createElement(Sections.AudienceSection, { project: makeProject(), onPatch: vi.fn(), busy: false }));
    expect(section('accessGroupAudienceTitle')).toBeNull();
  });

  it('admin-set rows → the note list only (no control); × removes the row', async () => {
    setSession({ ...OWNER }, makeProject());
    const onPatch = vi.fn();
    await renderTree(createElement(Sections.AudienceSection, {
      project: makeProject({ audiences: [row('public', 'readonly'), row('group:g9', 'full')] }),
      onPatch, busy: false,
    }));
    expect(section('accessGroupAudienceTitle')).not.toBeNull();
    expect(segmentedProps).toBeNull();
    const notes = Array.from(container.querySelectorAll('[data-audience-note]'))
      .map(n => n.getAttribute('data-audience-note'));
    expect(notes).toEqual(['public', 'group:g9']);
    await act(async () => { (container.querySelector('[data-audience-note="group:g9"] button') as HTMLButtonElement).click(); });
    expect(onPatch).toHaveBeenCalledWith({ audiences: [row('public', 'readonly')] });
  });
});

describe('AudienceSection — visibility', () => {
  it('a non-owner non-admin sees no section, even with rows present', async () => {
    setSession(
      { ...OWNER, group_subject: 'mod:m1' },
      makeProject({ is_owner_like: false, owner_id: 'u2', audiences: [row('public', 'readonly')] }),
    );
    await renderTree(createElement(Sections.AudienceSection, { project: makeProject({ is_owner_like: false, owner_id: 'u2', audiences: [row('public', 'readonly')] }), onPatch: vi.fn(), busy: false }));
    expect(section('accessGroupAudienceTitle')).toBeNull();
  });
});

describe('ProjectSettingsPanel — a failed audiences PATCH', () => {
  it('toasts and leaves the rows at the stored list (the section is controlled)', async () => {
    setSession({ ...ADMIN }, makeProject({ audiences: [row('mod:m1', 'readonly')] }));
    patchMock.mockRejectedValue(new Error('boom'));
    const { ProjectSettingsPanel } = await import('./ProjectSettingsPanel');
    await renderTree(createElement(ProjectSettingsPanel));
    const level = levelPickers()[0]!;
    await act(async () => { level.onSelect('full'); });
    expect(patchMock).toHaveBeenCalledWith('/projects/p1', { audiences: [row('mod:m1', 'full')] });
    expect(showToast).toHaveBeenCalledWith('failedToSaveSettings');
    // Previous rows: the store was never patched, so the level still reads the stored one.
    expect(levelPickers()[0]!.value).toBe('readonly');
  });
});

describe('MembersSection — the invite row', () => {
  it('group-bound owner: one filterable roster picker, no free-text field; a roster pick invites', async () => {
    setSession(
      { ...OWNER, group_subject: 'mod:m1' },
      makeProject(),
    );
    (apiAccess.fetchGroupRoster as ReturnType<typeof vi.fn>).mockResolvedValue([
      { user_id: 'u2', name: 'Bob', email: 'bob@x.test' },
    ]);
    await renderTree(createElement(Sections.MembersSection, { projectId: 'p1' }));
    expect(apiAccess.fetchGroupRoster).toHaveBeenCalled();
    // No email field at all — the roster picker IS the input (filter inside).
    expect(fieldInputProps).toBeNull();
    const picker = dropdownProps.find(d => d.options.some(o => o.value === 'bob@x.test'))!;
    expect(picker.options.map(o => o.value)).toEqual(['bob@x.test']);
    expect(picker.filterPlaceholder).toBe('accessInviteFilterPlaceholder');
    expect(picker.triggerLabel).toBe('accessGroupPickerLabel');
    expect(inviteButton().disabled).toBe(true);

    // Picking from the roster fills the email; the invite goes through.
    await act(async () => { picker.onSelect('bob@x.test'); });
    const picked = [...dropdownProps].reverse().find(d => d.filterPlaceholder)!;
    expect(picked.triggerLabel).toBe('Bob');
    await act(async () => { inviteButton().click(); });
    expect(apiAccess.inviteMember).toHaveBeenCalledWith('p1', 'bob@x.test', 'readonly');
  });

  /** The latest render of the invite-role Dropdown (its options are the three levels). */
  function inviteRolePicker() {
    return [...dropdownProps].reverse().find(d =>
      !d.filterPlaceholder && d.options.map(o => o.value).join() === 'readonly,commentator,full')!;
  }

  it('project open to the own group at commentator: levels the group already gives are disabled, the invite goes out at the first higher one', async () => {
    setSession(
      { ...OWNER, group_subject: 'mod:m1' },
      makeProject({ audiences: [row('mod:m1', 'commentator')] }),
    );
    (apiAccess.fetchGroupRoster as ReturnType<typeof vi.fn>).mockResolvedValue([
      { user_id: 'u2', name: 'Bob', email: 'bob@x.test' },
    ]);
    await renderTree(createElement(Sections.MembersSection, { projectId: 'p1' }));
    const role = inviteRolePicker();
    expect(role.options.filter(o => o.disabled).map(o => o.value)).toEqual(['readonly', 'commentator']);
    expect(role.options.find(o => o.value === 'readonly')!.note).toBe('accessViaGroupNote');
    expect(role.value).toBe('full');
    const picker = [...dropdownProps].reverse().find(d => d.filterPlaceholder)!;
    await act(async () => { picker.onSelect('bob@x.test'); });
    await act(async () => { inviteButton().click(); });
    expect(apiAccess.inviteMember).toHaveBeenCalledWith('p1', 'bob@x.test', 'full');
  });

  it('group gives full: every level disabled, Invite disabled, the reason is shown', async () => {
    setSession(
      { ...OWNER, group_subject: 'mod:m1' },
      makeProject({ audiences: [row('mod:m1', 'full')] }),
    );
    (apiAccess.fetchGroupRoster as ReturnType<typeof vi.fn>).mockResolvedValue([
      { user_id: 'u2', name: 'Bob', email: 'bob@x.test' },
    ]);
    await renderTree(createElement(Sections.MembersSection, { projectId: 'p1' }));
    expect(inviteRolePicker().options.every(o => o.disabled)).toBe(true);
    const picker = [...dropdownProps].reverse().find(d => d.filterPlaceholder)!;
    await act(async () => { picker.onSelect('bob@x.test'); });
    expect(inviteButton().disabled).toBe(true);
    expect(container.textContent).toContain('accessInviteCoveredByGroup');
  });

  it('own-group floor reads the OWN-subject row only: a foreign audience disables nothing', async () => {
    setSession(
      { ...OWNER, group_subject: 'mod:m2' },
      makeProject({ audiences: [row('mod:m1', 'full')] }),
    );
    await renderTree(createElement(Sections.MembersSection, { projectId: 'p1' }));
    expect(inviteRolePicker().options.some(o => o.disabled)).toBe(false);
  });

  it('member rows: a group member whose row is below the group level carries the via-group badge; others do not', async () => {
    setSession(
      { ...OWNER, group_subject: 'mod:m1' },
      makeProject({ audiences: [row('mod:m1', 'commentator')] }),
    );
    (apiAccess.fetchGroupRoster as ReturnType<typeof vi.fn>).mockResolvedValue([
      { user_id: 'u2', name: 'Bob', email: 'bob@x.test' },
      { user_id: 'u3', name: 'Cid', email: 'cid@x.test' },
    ]);
    (apiAccess.listMembers as ReturnType<typeof vi.fn>).mockResolvedValue([
      { user_id: 'u2', name: 'Bob', email: 'bob@x.test', access_level: 'readonly' },
      { user_id: 'u3', name: 'Cid', email: 'cid@x.test', access_level: 'full' },
      { user_id: 'u9', name: 'Out', email: 'out@x.test', access_level: 'readonly' },
    ]);
    await renderTree(createElement(Sections.MembersSection, { projectId: 'p1' }));
    const badges = Array.from(container.querySelectorAll('[data-via-group]'))
      .map(b => b.getAttribute('data-via-group'));
    expect(badges).toEqual(['u2']);
  });

  it('roster picker hides accounts already in the project — members and pending invites alike', async () => {
    setSession({ ...OWNER, group_subject: 'mod:m1' }, makeProject());
    (apiAccess.fetchGroupRoster as ReturnType<typeof vi.fn>).mockResolvedValue([
      { user_id: 'u2', name: 'Bob', email: 'bob@x.test' },
      { user_id: 'u3', name: 'Cid', email: 'cid@x.test' },
      { user_id: 'u4', name: 'Dee', email: 'dee@x.test' },
    ]);
    (apiAccess.listMembers as ReturnType<typeof vi.fn>).mockResolvedValue([
      { user_id: 'u2', name: 'Bob', email: 'bob@x.test', access_level: 'readonly' },
      { user_id: '', name: '', email: 'Cid@x.test', access_level: 'readonly', pending: true },
    ]);
    await renderTree(createElement(Sections.MembersSection, { projectId: 'p1' }));
    const picker = [...dropdownProps].reverse().find(d => d.filterPlaceholder)!;
    expect(picker.options.map(o => o.value)).toEqual(['dee@x.test']);
  });

  it('no-group owner: silent free-text invite, roster never fetched', async () => {
    setSession({ ...OWNER }, makeProject());
    await renderTree(createElement(Sections.MembersSection, { projectId: 'p1' }));
    expect(apiAccess.fetchGroupRoster).not.toHaveBeenCalled();
    await act(async () => { fieldInputProps!.onChange({ target: { value: 'anyone@x.test' } }); });
    await act(async () => { inviteButton().click(); });
    expect(apiAccess.inviteMember).toHaveBeenCalledWith('p1', 'anyone@x.test', 'readonly');
    expect(showToast).not.toHaveBeenCalledWith('accessInviteOutsideGroup');
  });
});

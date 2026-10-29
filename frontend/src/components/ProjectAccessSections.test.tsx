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
});

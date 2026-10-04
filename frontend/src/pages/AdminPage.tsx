/** Admin panel: tab-less section on the SectionShell.
 * Store slices: currentUser; ui-store global prefs (the admin last section +
 * the aside width shared with the project sidebar and the cabinet — ONE width,
 * panelWidths.left — persisted per user on /preferences/_global).
 */
// ARCH: Thin shell — SectionShell owns the chrome with NO top tabs: the gear
// button in UserControls is the section's tab, the aside lists the sections
// (Users / Projects / Embeddings + the admin-only settings tabs and Skills)
// as rows, the center carries the active section's FULL list. All domain
// handlers live here; the list renderers (ProjectsTab, UsersTab) render only.
// The settings/skills sections fetch from inside their own components (an
// admin-only mount), so a moderator's page open fires no 403-gated request.
//
// List loading: projects are SERVER-filtered (`q` — client-side
// filtering over a paged list hides unloaded matches) and scroll-paged in 100s
// from the Projects section's first activation; users load once on mount
// (limit 1000 — the Projects member picker needs the full list) and filter
// client-side by name OR email. Every fetch failure shows an error line above
// the list plus a toast — no `.catch(console.error)` anywhere.

import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';
import { FolderOpen, Users as UsersIcon, Layers, Cpu, Bot, Wrench, Search, Database, Sparkles, Gauge } from 'lucide-react';
import { apiClient } from '../api/client';
import { listAdminProjects, listAdminUsers } from '../api/admin';
import type { SettingsTabId } from '../api/admin';
import { useAppStore } from '../store/app-store';
import { useUIStore, type AdminSectionTab } from '../store/ui-store';
import { useSectionAsideWidth } from '../hooks/useSectionAsideWidth';
import { AccessLevel, Role, User, Project } from '../types';
import { SectionShell, SectionAsideRow } from '../components/SectionShell';
import { CenterHeading, FieldInput } from '../components/ui';
import { useTranslation } from '../i18n';
import type { TranslationKey } from '../i18n/en';
import { ProjectsTab } from '../components/admin/ProjectsTab';
import { UsersTab, UserUpdates } from '../components/admin/UsersTab';
import { EmbeddingsTab } from '../components/admin/EmbeddingsTab';
import { InfoTab } from '../components/admin/InfoTab';
import { SettingsSection } from '../components/admin/SettingsSection';
import { SkillsSection } from '../components/admin/SkillsSection';
import { withOptimistic } from '../utils/optimistic';
import { serverRefusalDetail } from '../utils/server-refusal-detail';
import { AddUserForm } from '../components/admin/AddUserForm';

type AdminTab = AdminSectionTab;

/** The admin-only sections, appended after Users / Projects in this order —
 * ONE row per section feeds the aside rows, the saved-section validation
 * list (sectionsAtMount) and the settings-tab lookup. The tab LIST is fixed
 * here, never derived from the server (a new section needs an i18n label
 * and an icon anyway); the AdminSectionTab union in theme-slice stays — it
 * is the persisted-value type. */
const ADMIN_ONLY_SECTIONS: readonly {
  tab: AdminSectionTab;
  icon: ReactNode;
  labelKey: TranslationKey;
  /** `settings:<registry tab>` rows carry the registry tab id for the center. */
  settingsTab?: SettingsTabId;
}[] = [
  { tab: 'info', icon: <Gauge size={14} />, labelKey: 'adminInfo' },
  { tab: 'embeddings', icon: <Layers size={14} />, labelKey: 'embeddings' },
  { tab: 'settings:models', icon: <Cpu size={14} />, labelKey: 'settingsModels', settingsTab: 'models' },
  { tab: 'settings:search', icon: <Search size={14} />, labelKey: 'settingsSearch', settingsTab: 'search' },
  { tab: 'settings:tools', icon: <Wrench size={14} />, labelKey: 'settingsTools', settingsTab: 'tools' },
  { tab: 'settings:agent', icon: <Bot size={14} />, labelKey: 'settingsAgent', settingsTab: 'agent' },
  { tab: 'settings:storage', icon: <Database size={14} />, labelKey: 'settingsStorage', settingsTab: 'storage' },
  { tab: 'skills', icon: <Sparkles size={14} />, labelKey: 'skills' },
];

/** Projects page size — a full page means "maybe more", so the sentinel stays armed. */
const PROJECT_PAGE = 100;
/** Filter debounce (ms) — one request per pause in typing, not per keystroke. */
const FILTER_DEBOUNCE_MS = 200;

/** Users: single fetch, whole list (few rows; the member picker needs them all). */
const USERS_LIMIT = 1000;

/** Server-filtered, scroll-paged admin project list.
 *
 * The first fetch fires on the tab's FIRST activation (not on /admin mount);
 * every committed query change REPLACES the list from offset 0. A seq guard
 * drops pages that resolve after a newer request — without it a slow in-flight
 * page could append stale rows under a new query.
 */
function useAdminProjects() {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const [debouncedQ, setDebouncedQ] = useState('');
  const [activated, setActivated] = useState(false);
  const [projects, setProjects] = useState<Project[]>([]);
  const [loadFailed, setLoadFailed] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);

  const seq = useRef(0);
  const activatedOnce = useRef(false);
  // Read by the IntersectionObserver callback, which must keep one identity for
  // the page's lifetime (re-creating it would re-observe the sentinel per render).
  const latest = useRef({ hasMore: false, loading: false, q: '', count: 0 });
  useEffect(() => {
    latest.current = { hasMore, loading, q: debouncedQ, count: projects.length };
  });

  const loadPage = useCallback(async (q: string, offset: number) => {
    const id = ++seq.current;
    setLoading(true);
    try {
      const page = await listAdminProjects({ q: q || undefined, offset, limit: PROJECT_PAGE });
      if (id !== seq.current) return;
      setProjects(prev => (offset === 0 ? page : [...prev, ...page]));
      setHasMore(page.length === PROJECT_PAGE);
      setLoadFailed(false);
    } catch {
      if (id !== seq.current) return;
      setLoadFailed(true);
      useAppStore.getState().showToast(t('adminListLoadFailed'), 'error');
    } finally {
      if (id === seq.current) setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    const h = setTimeout(() => setDebouncedQ(query), FILTER_DEBOUNCE_MS);
    return () => clearTimeout(h);
  }, [query]);

  // First activation and every committed query change: replace from page 0.
  useEffect(() => {
    if (!activated) return;
    void loadPage(debouncedQ, 0);
  }, [activated, debouncedQ, loadPage]);

  /** Idempotent — called on every Projects-tab activation, fetches only the first. */
  const activate = useCallback(() => {
    if (activatedOnce.current) return;
    activatedOnce.current = true;
    setActivated(true);
  }, []);

  // Sentinel wiring: one observer for the page's lifetime; the ref callback
  // re-targets it as the projects list (re)mounts across section switches. jsdom
  // has no IntersectionObserver — the guard keeps tests without a stub working.
  const observerRef = useRef<IntersectionObserver | null>(null);
  const ensureObserver = useCallback(() => {
    if (observerRef.current) return observerRef.current;
    if (typeof IntersectionObserver === 'undefined') return null;
    observerRef.current = new IntersectionObserver(entries => {
      if (!entries.some(e => e.isIntersecting)) return;
      const { hasMore: more, loading: busy, q, count } = latest.current;
      if (!more || busy) return;
      void loadPage(q, count);
    });
    return observerRef.current;
  }, [loadPage]);
  useEffect(() => () => observerRef.current?.disconnect(), []);

  const sentinelRef = useCallback((el: HTMLDivElement | null) => {
    observerRef.current?.disconnect();
    if (el) ensureObserver()?.observe(el);
  }, [ensureObserver]);

  return { query, setQuery, projects, setProjects, loadFailed, activate, sentinelRef };
}

export function AdminPage() {
  const navigate = useNavigate();
  const currentUser = useAppStore(s => s.currentUser);

  // Per-user persisted section prefs (global prefs are hydrated before this
  // page mounts — AuthGuard gates children on loadGlobalPrefs). The aside width
  // is panelWidths.left — ONE width with the project sidebar and the cabinet's
  // aside (useSectionAsideWidth).
  const savedSectionTab = useUIStore(s => s.adminSectionTab);
  const setAdminSectionTab = useUIStore(s => s.setAdminSectionTab);
  const { initialAsideWidth, onAsideWidthChange } = useSectionAsideWidth();

  const [users, setUsers] = useState<User[]>([]);
  const [usersLoadFailed, setUsersLoadFailed] = useState(false);
  const [usersQuery, setUsersQuery] = useState('');
  const isAdmin = currentUser?.is_admin ?? false;

  // Initial section: the user's last one, validated against the sections THIS
  // user is offered (a moderator has no Embeddings/settings/skills rows — a
  // stale saved admin-only tab must not open an admin-only surface). Derived,
  // not mutated: the store keeps the raw value, the page falls back to Users.
  // Mount-time snapshot by design — later store changes don't yank the open
  // section.
  const sectionsAtMount: AdminTab[] = isAdmin
    ? ['users', 'projects', ...ADMIN_ONLY_SECTIONS.map(s => s.tab)]
    : ['users', 'projects'];
  const [tab, setTab] = useState<AdminTab>(() =>
    savedSectionTab && sectionsAtMount.includes(savedSectionTab) ? savedSectionTab : 'users');

  /** Section row click: switch + persist the choice (per-user, _global prefs). */
  const selectSection = useCallback((next: AdminTab) => {
    setTab(next);
    setAdminSectionTab(next);
  }, [setAdminSectionTab]);
  // INVARIANT: the group selects' moderator options are DERIVED from `users`,
  // never fetched separately.  Why: a separate fetch goes stale the moment a
  // role is changed on this page — promoting a user then assigning another
  // user to that new group in the same sitting offered no such group.
  const moderators = useMemo(
    () => (isAdmin ? users.filter(u => u.role === 'moderator') : []),
    [users, isAdmin],
  );
  // Member candidates for the Projects section. A moderator's user list is
  // the group WITHOUT themself, yet they may grant themself access (self is in
  // the backend scope set) — so self is prepended when the list lacks it.
  const memberCandidates = useMemo(
    () => (currentUser && !users.some(u => u.user_id === currentUser.user_id)
      ? [currentUser, ...users]
      : users),
    [users, currentUser],
  );

  const {
    query: projectsQuery,
    setQuery: setProjectsQuery,
    projects,
    setProjects,
    loadFailed: projectsLoadFailed,
    activate: activateProjects,
    sentinelRef,
  } = useAdminProjects();

  const [expandedProject, setExpandedProject] = useState<string | null>(null);
  const [projectMembers, setProjectMembers] = useState<Record<string, Record<string, string>>>({});

  const { t } = useTranslation();

  useEffect(() => {
    // Capability flag (admin | moderator), never a role comparison: a demoted
    // moderator's /me refresh flips the flag and the redirect follows.
    if (!currentUser?.can_manage_users) {
      navigate('/');
      return;
    }
    let cancelled = false;
    listAdminUsers({ limit: USERS_LIMIT })
      .then(rows => { if (!cancelled) { setUsers(rows); setUsersLoadFailed(false); } })
      .catch(() => {
        if (cancelled) return;
        setUsersLoadFailed(true);
        useAppStore.getState().showToast(t('adminListLoadFailed'), 'error');
      });
    return () => { cancelled = true; };
  }, [currentUser, navigate, t]);

  // The projects pipeline starts on the FIRST Projects-section activation
  // (not on mount — Users opens first); later activations keep the
  // already-loaded (possibly filtered/paged) list.
  useEffect(() => {
    if (tab === 'projects') activateProjects();
  }, [tab, activateProjects]);

  const loadProjectMembers = async (projectId: string) => {
    const members = await apiClient.get(`/admin/projects/${projectId}/members`);
    setProjectMembers(prev => ({ ...prev, [projectId]: members }));
  };

  const toggleProject = async (projectId: string) => {
    if (expandedProject === projectId) {
      setExpandedProject(null);
    } else {
      setExpandedProject(projectId);
      if (!projectMembers[projectId]) {
        await loadProjectMembers(projectId);
      }
    }
  };

  /** Toasts the refusal (duplicate email, short password, …) and rethrows so
   * the form keeps its draft instead of clearing on a failed create. */
  const handleAddUser = async (data: { name: string; email: string; password: string; role: Role; moderator_id: string | null }) => {
    try {
      const user = await apiClient.post('/admin/users', data);
      setUsers(prev => [...prev, user]);
    } catch (err: unknown) {
      useAppStore.getState().showToast(serverRefusalDetail(err) ?? t('failedToCreateUser'), 'error');
      throw err;
    }
  };

  /** Mint a single-use invite token and copy the /register link to the
   * clipboard (both roles). The URL is composed from THIS page's origin —
   * the origin the admin can reach is by definition the origin the invitee
   * can reach; the backend returns only {token, expires_at}. */
  const handleInvite = async () => {
    try {
      const { token } = await apiClient.post('/invites', {});
      if (token) {
        await navigator.clipboard?.writeText(`${window.location.origin}/register/${token}`);
      }
      useAppStore.getState().showToast(t('inviteLinkCopied'), 'info');
    } catch {
      useAppStore.getState().showToast(t('failedToCreateInvite'), 'error');
    }
  };

  const handleDeleteUser = async (userId: string) => {
    try {
      await apiClient.delete(`/admin/users/${userId}`);
      setUsers(prev => prev.filter(u => u.user_id !== userId));
    } catch (err: unknown) {
      useAppStore.getState().showToast(err instanceof Error ? err.message : t('failedToDeleteUser'));
    }
  };

  const handleSaveUser = async (user: User, updates: UserUpdates) => {
    if (!Object.keys(updates).length) return;
    try {
      const updated = await apiClient.patch(`/admin/users/${user.user_id}`, updates);
      setUsers(prev => prev.map(u => u.user_id === user.user_id ? updated : u));
    } catch (err: unknown) {
      useAppStore.getState().showToast(serverRefusalDetail(err) ?? t('failedToUpdateUser'));
    }
  };

  const handleSetAccess = async (projectId: string, userId: string, access: AccessLevel | null) => {
    try {
      if (access === null) {
        await apiClient.delete(`/admin/projects/${projectId}/members/${userId}`);
      } else {
        await apiClient.post(`/admin/projects/${projectId}/members`, { user_id: userId, access_level: access });
      }
      await loadProjectMembers(projectId);
      // A change on MY OWN membership moves the open-project button: recompute
      // the row's my_access with the SAME predicate the server list uses
      // (owner full / member level, admin-elevated / public readonly / none)
      // so the button updates without a reload.
      if (userId === currentUser?.user_id) {
        setProjects(prev => prev.map(p =>
          p.project_id !== projectId || p.owner_id === currentUser.user_id ? p : {
            ...p,
            my_access: access === null ? (p.is_public ? 'readonly' : null) : (isAdmin ? 'full' : access),
          }));
      }
    } catch {
      useAppStore.getState().showToast(t('failedToUpdateAccess'));
    }
  };

  /** Transfer project ownership: POST, re-fetch the member list, then patch
   * the owner and the members together. Resolves false on refusal (toast +
   * unchanged list — no silent degradation). `my_access` needs no patch: old
   * and new owner both resolve to `full`. */
  const handleTransferOwner = async (projectId: string, userId: string): Promise<boolean> => {
    let res: { owner_id: string; owner_name: string | null };
    try {
      res = await apiClient.post(`/admin/projects/${projectId}/owner`, { user_id: userId });
    } catch {
      useAppStore.getState().showToast(t('failedToUpdateAccess'), 'error');
      return false;
    }
    const patchOwner = () => setProjects(prev => prev.map(p =>
      p.project_id === projectId
        ? { ...p, owner_id: res.owner_id, owner_name: res.owner_name }
        : p));
    try {
      const members = await apiClient.get(`/admin/projects/${projectId}/members`);
      // WHY: both setters in one synchronous tick — patching owner_id before
      // the members arrive renders a window where the new owner is already
      // filtered out and the old one is not yet a member: one row vanishes
      // and comes back.
      patchOwner();
      setProjectMembers(prev => ({ ...prev, [projectId]: members }));
    } catch {
      // The transfer happened; show the true owner and say the list is stale.
      patchOwner();
      useAppStore.getState().showToast(t('failedToLoadMembers'), 'error');
    }
    return true;
  };

  const handleRenameProject = async (project: Project, newName: string) => {
    await withOptimistic(
      { ...project, name: newName },
      project,
      (p) => setProjects(prev => prev.map(x => x.project_id === p.project_id ? p : x)),
      () => apiClient.patch(`/projects/${project.project_id}`, { name: newName }),
    );
  };

  const handleDeleteProject = async (project: Project) => {
    try {
      await apiClient.delete(`/projects/${project.project_id}`);
      setProjects(prev => prev.filter(p => p.project_id !== project.project_id));
    } catch {
      useAppStore.getState().showToast(t('failedToDeleteProject'));
    }
  };

  const handleTogglePublic = async (project: Project) => {
    const newValue = !project.is_public;
    try {
      const updated = await apiClient.patch(`/projects/${project.project_id}`, { is_public: newValue });
      setProjects(prev => prev.map(p => p.project_id === project.project_id ? { ...p, ...updated } : p));
    } catch {
      useAppStore.getState().showToast(t('failedToUpdateProject'));
    }
  };

  // Client-side users filter: name OR email (email is the unique handle admins
  // search by). Projects must NOT work this way — see useAdminProjects.
  const usersNeedle = usersQuery.trim().toLowerCase();
  const visibleUsers = usersNeedle
    ? users.filter(u => u.name.toLowerCase().includes(usersNeedle) || u.email.toLowerCase().includes(usersNeedle))
    : users;

  /** Load-failure line above the list it concerns. */
  const errorLine = (
    <p className="text-xs text-red mb-2">{t('adminListLoadFailed')}</p>
  );

  /** Escape in either filter input clears the query and blurs (plan decision). */
  const filterKeyDown = (clear: () => void) => (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') {
      clear();
      e.currentTarget.blur();
    }
  };

  // Embeddings, the settings tabs and Skills are admin-only (capability
  // flag, not a role comparison). Flat rows beside the others — a section is
  // a heading in the center, never a second navigation level.
  const sections: { tab: AdminTab; icon: ReactNode; label: string }[] = [
    { tab: 'users', icon: <UsersIcon size={14} />, label: t('users') },
    { tab: 'projects', icon: <FolderOpen size={14} />, label: t('projects') },
    ...(isAdmin ? ADMIN_ONLY_SECTIONS.map(s => ({ tab: s.tab, icon: s.icon, label: t(s.labelKey) })) : []),
  ];
  const settingsTab = ADMIN_ONLY_SECTIONS.find(s => s.tab === tab)?.settingsTab;

  // Header crumb: `Admin panel : <section>` follows the ACTIVE tab (not the
  // persisted one — see the mount fallback above); cleared when the page unmounts.
  const setSectionCrumb = useAppStore(s => s.setSectionCrumb);
  const activeLabel = sections.find(s => s.tab === tab)?.label ?? null;
  useEffect(() => {
    setSectionCrumb(activeLabel);
    return () => setSectionCrumb(null);
  }, [activeLabel, setSectionCrumb]);

  const renderAside = () => (
    <div className="px-1 pt-1">
      {sections.map(s => (
        <SectionAsideRow key={s.tab} label={s.label} icon={s.icon} selected={tab === s.tab} onSelect={() => selectSection(s.tab)} />
      ))}
    </div>
  );

  return (
    <SectionShell
      tabs={[]}
      renderAside={renderAside}
      initialAsideWidth={initialAsideWidth}
      onAsideWidthChange={onAsideWidthChange}
      renderCenter={() => (
        <div className="h-full overflow-y-auto">
          <div className="max-w-[800px] mx-auto py-12 px-8">
            {tab === 'projects' && (
              <>
                <CenterHeading>{t('projects')}</CenterHeading>
                {projectsLoadFailed && errorLine}
                <div className="mb-3">
                  <FieldInput
                    className="w-full"
                    placeholder={t('filterProjects')}
                    value={projectsQuery}
                    onChange={e => setProjectsQuery(e.target.value)}
                    onKeyDown={filterKeyDown(() => setProjectsQuery(''))}
                  />
                </div>
                <ProjectsTab
                  projects={projects}
                  users={memberCandidates}
                  onRename={handleRenameProject}
                  onDelete={handleDeleteProject}
                  onSetAccess={handleSetAccess}
                  onTransferOwner={handleTransferOwner}
                  onTogglePublic={handleTogglePublic}
                  onToggleExpand={toggleProject}
                  expandedProject={expandedProject}
                  projectMembers={projectMembers}
                />
                {/* Scroll-paging sentinel: intersecting requests the next page. */}
                <div ref={sentinelRef} className="h-px" aria-hidden="true" />
              </>
            )}

            {tab === 'users' && (
              <>
                <AddUserForm onAdd={handleAddUser} onInvite={handleInvite} isAdmin={isAdmin} moderators={moderators} />
                <CenterHeading>{t('users')}</CenterHeading>
                {usersLoadFailed && errorLine}
                <div className="mb-3">
                  <FieldInput
                    className="w-full"
                    placeholder={t('filterUsers')}
                    value={usersQuery}
                    onChange={e => setUsersQuery(e.target.value)}
                    onKeyDown={filterKeyDown(() => setUsersQuery(''))}
                  />
                </div>
                <UsersTab
                  users={visibleUsers}
                  currentUserId={currentUser?.user_id}
                  onSave={handleSaveUser}
                  onDelete={handleDeleteUser}
                  isAdmin={isAdmin}
                  moderators={moderators}
                />
              </>
            )}

            {tab === 'info' && <InfoTab />}
            {tab === 'embeddings' && <EmbeddingsTab />}
            {settingsTab !== undefined && <SettingsSection tab={settingsTab} />}
            {tab === 'skills' && <SkillsSection />}
          </div>
        </div>
      )}
    />
  );
}

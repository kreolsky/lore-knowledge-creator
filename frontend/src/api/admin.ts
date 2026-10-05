/** Admin REST wrappers: server-filtered, scroll-paged project list, one-shot
 *  user list, and the instance settings/skills/model-access surfaces (admin-only
 *  sections).
 *
 *  The project/user lists are the only admin reads with parameters — mutations,
 *  members and per-entity admin calls stay inline via apiClient at their call
 *  sites.
 */

import { apiClient } from './client';
import type { Project, User } from '../types';

export interface AdminProjectsQuery {
  /** Name substring, matched case-insensitively server-side; empty → no filter. */
  q?: string;
  offset?: number;
  limit?: number;
}

/** One page of ALL projects (admin), `q`-filtered, `created_at DESC`. */
export async function listAdminProjects({ q, offset = 0, limit = 100 }: AdminProjectsQuery = {}): Promise<Project[]> {
  const params = new URLSearchParams();
  if (q) params.set('q', q);
  params.set('offset', String(offset));
  params.set('limit', String(limit));
  return await apiClient.get(`/admin/projects?${params}`) as Project[];
}

/** All active users in one shot — the Projects member picker needs the full
 *  list, and the admin's moderator options are derived from it (AdminPage). */
export async function listAdminUsers({ limit = 1000 }: { limit?: number } = {}): Promise<User[]> {
  const params = new URLSearchParams();
  params.set('limit', String(limit));
  return await apiClient.get(`/admin/users?${params}`) as User[];
}

// ── Instance settings (SYSTEM: instance-settings) ──────────────────────────

/** Registry tab ids — must equal backend settings_registry.TABS (the ids after
 * the `settings:` prefix of AdminSectionTab). */
export type SettingsTabId = 'models' | 'search' | 'tools' | 'agent' | 'storage';

/** One editable instance setting — a GET /api/admin/settings row, derived from
 * the backend registry at call time. `value` is the EFFECTIVE value (a row
 * override if present, else the process's env-effective config value); secrets
 * arrive masked ("••••" + last 4). */
export interface AdminSettingEntry {
  key: string;
  env: string;
  tab: SettingsTabId;
  section: string;
  /** `text` = a multiline string (a pasted workflow, a prompt). */
  type: 'int' | 'float' | 'bool' | 'str' | 'text' | 'secret';
  label: string;
  help: string;
  effect: 'live' | 'restart';
  min: number | null;
  max: number | null;
  /** A closed value list: the control is a Dropdown, the server refuses the rest. */
  choices: string[] | null;
  /** Show this row only while `key`'s saved value equals `value`. */
  visible_if: { key: string; value: string } | null;
  source: 'default' | '.env' | 'override';
  value: string | number | boolean;
}

export async function listAdminSettings(): Promise<AdminSettingEntry[]> {
  const data = await apiClient.get('/admin/settings') as { settings: AdminSettingEntry[] };
  return data.settings;
}

/** Write one override row; resolves to the updated entry (masked secrets stay
 * masked). Sending an unchanged masked secret is a server-side no-op. */
export async function putAdminSetting(key: string, value: unknown): Promise<AdminSettingEntry> {
  return await apiClient.put(`/admin/settings/${key}`, { value }) as AdminSettingEntry;
}

/** Reset to env/default: the row is REMOVED server-side. */
export async function resetAdminSetting(key: string): Promise<void> {
  await apiClient.delete(`/admin/settings/${key}`);
}

// ── Instance skills ─────────────────────────────────────────────────────────

/** One union row of shipped files ⊕ instance_skills. `content` is the INSTANCE
 * row's own content (null for pure tombstones and untouched shipped skills —
 * the UI offers Override there, never Edit); `shipped_body` rides for the
 * read-only view and the override copy; `shipped` says DELETE restores it. */
export interface AdminSkillEntry {
  name: string;
  source: 'shipped' | 'instance';
  shipped: boolean;
  enabled: boolean;
  content: string | null;
  shipped_body: string | null;
  updated_by: string | null;
  updated_at: string | null;
}

export async function listAdminSkills(): Promise<AdminSkillEntry[]> {
  const data = await apiClient.get('/admin/skills') as { skills: AdminSkillEntry[] };
  return data.skills;
}

/** Upsert one instance skill by name — content (frontmatter name must equal
 * the path name) and/or enabled (enabled=false contentless = a tombstone). */
export async function putAdminSkill(name: string, body: { content?: string; enabled?: boolean }): Promise<AdminSkillEntry> {
  return await apiClient.put(`/admin/skills/${name}`, body) as AdminSkillEntry;
}

/** Remove the instance row — an overridden/tombstoned shipped skill reappears,
 * a new skill disappears. */
export async function deleteAdminSkill(name: string): Promise<void> {
  await apiClient.delete(`/admin/skills/${name}`);
}

// ── Instance info (admin Info section) ─────────────────────────────────────

/** Per-table storage stats from GET /api/admin/info/storage. The bytes are a
 *  LOGICAL estimate (serialized row length), never disk usage — the UI must
 *  label them as an estimate and show the disk number separately. The deleted
 *  and in-deleted-projects buckets are disjoint (a row both soft-deleted and
 *  in a dead project counts once, in `deleted`). */
export interface AdminStorageTableStats {
  name: string;
  live_rows: number;
  live_bytes: number;
  deleted_rows: number;
  deleted_bytes: number;
  in_deleted_projects_rows: number;
  in_deleted_projects_bytes: number;
}

export interface AdminStorageInfo {
  /** Null = the DB directory is not mounted; `disk_error` names the path. */
  disk: { db_bytes: number; volume_bytes: number } | null;
  disk_error: string | null;
  tables: AdminStorageTableStats[];
  totals: Omit<AdminStorageTableStats, 'name'>;
  measured_ms: number;
  /** ISO time the numbers were taken — a memoized answer can be up to 30 min old. */
  measured_at: string;
}

/** Served from the backend's 30-min memo; `fresh` forces a new measurement. */
export async function getAdminStorageInfo(fresh = false): Promise<AdminStorageInfo> {
  return await apiClient.get(`/admin/info/storage${fresh ? '?fresh=1' : ''}`) as AdminStorageInfo;
}

// ── Model access (SYSTEM: model-access) ────────────────────────────────────

/** One model row: the gateway roster ∪ every id that carries a grant. A
 * subject is `public`, `mod:<moderator uid>` or `group:<admin group id>`;
 * the default model is available to everyone without any subject. */
export interface AdminModelAccess {
  id: string;
  /** False for a granted id the gateway no longer serves; null on every row
   * when the gateway could not be read (the grants are still listed). */
  in_gateway: boolean | null;
  is_default: boolean;
  subjects: string[];
}

/** The subject that makes a model available to every user. */
export const PUBLIC_SUBJECT = 'public';

export async function listAdminModels(): Promise<AdminModelAccess[]> {
  return await apiClient.get('/admin/models') as AdminModelAccess[];
}

/** Full replace of a model's subjects. Ids carry `/` — the route is `:path`,
 * so each segment is encoded and the slashes kept. */
export async function putAdminModelSubjects(modelId: string, subjects: string[]): Promise<{ id: string; subjects: string[] }> {
  const path = modelId.split('/').map(encodeURIComponent).join('/');
  return await apiClient.put(`/admin/models/${path}`, { subjects });
}

/** An admin group (stored, editable) or a moderator group (virtual, derived
 * from `users.moderator_id`, read-only; `name` is the moderator's name). */
export interface AdminGroup {
  /** Admin group: its id. Moderator group: `mod:<uid>`. */
  id: string;
  /** The grant subject this group is addressed by. */
  subject: string;
  kind: 'admin' | 'moderator';
  name: string;
  members: { user_id: string; name: string }[];
  models: string[];
}

export async function listAdminGroups(): Promise<AdminGroup[]> {
  return await apiClient.get('/admin/groups') as AdminGroup[];
}

export async function createAdminGroup(name: string): Promise<AdminGroup> {
  return await apiClient.post('/admin/groups', { name }) as AdminGroup;
}

export async function renameAdminGroup(groupId: string, name: string): Promise<AdminGroup> {
  return await apiClient.patch(`/admin/groups/${groupId}`, { name }) as AdminGroup;
}

/** Deletes the group with its memberships and its grants. */
export async function deleteAdminGroup(groupId: string): Promise<void> {
  await apiClient.delete(`/admin/groups/${groupId}`);
}

/** Full replace of an admin group's members. */
export async function putAdminGroupMembers(groupId: string, userIds: string[]): Promise<AdminGroup> {
  return await apiClient.put(`/admin/groups/${groupId}/members`, { user_ids: userIds }) as AdminGroup;
}

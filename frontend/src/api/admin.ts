/** Admin REST wrappers: server-filtered, scroll-paged project list, one-shot
 *  user list, and the instance settings/skills surfaces (admin-only sections).
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

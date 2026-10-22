/** Owner-only project access management — members CRUD + pending invites. */
// SYSTEM: access-api — owner-gated REST wrappers for project members and pending invites (members in ProjectSettingsPanel under the Access tab).

import { apiClient } from './client';
import type { AccessLevel, GroupRosterEntry, ProjectMember } from '../types';

/** The caller's moderator group as a pick list — the invite picker's source
 * for a group-bound owner (backend group_roster; [] for an admin / no group). */
export async function fetchGroupRoster(): Promise<GroupRosterEntry[]> {
  return apiClient.get('/me/group-roster');
}

export async function listMembers(projectId: string): Promise<ProjectMember[]> {
  const data = await apiClient.get(`/projects/${projectId}/members`);
  return data.members;
}

export async function inviteMember(
  projectId: string, email: string, access_level: AccessLevel,
): Promise<{ queued: true }> {
  return apiClient.post(`/projects/${projectId}/members`, { email, access_level });
}

export async function patchMember(
  projectId: string, userId: string, access_level: AccessLevel,
): Promise<void> {
  await apiClient.patch(`/projects/${projectId}/members/${userId}`, { access_level });
}

export async function removeMember(projectId: string, userId: string): Promise<void> {
  await apiClient.delete(`/projects/${projectId}/members/${userId}`);
}

export async function cancelInvite(projectId: string, email: string): Promise<void> {
  await apiClient.delete(`/projects/${projectId}/invites?email=${encodeURIComponent(email)}`);
}

/** Cross-project subtree move: POST /api/documents/{id}/move (gated full on the
 *  doc's project side AND on the target project — backend owns both gates). */
export async function moveDocumentToProject(
  documentId: string,
  body: { target_project_id: string; parent_id: string | null },
): Promise<{ moved: number; document_ids: string[]; reference_ids: string[] }> {
  return apiClient.post(`/documents/${documentId}/move`, body);
}

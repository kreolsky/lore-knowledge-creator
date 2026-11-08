/** Project list REST wrapper: one per-user page of a dashboard section (`scope`), `created_at DESC`. */

import { apiClient } from './client';
import type { Project } from '../types';

export type ProjectScope = 'all' | 'own' | 'shared';

export interface ProjectsQuery {
  scope?: ProjectScope;
  offset?: number;
  limit?: number;
}

/** One page of the caller's visible projects; `page.length === limit` ⇒ more may follow. */
export async function listProjects({ scope = 'all', offset = 0, limit = 50 }: ProjectsQuery = {}): Promise<Project[]> {
  const params = new URLSearchParams();
  params.set('scope', scope);
  params.set('offset', String(offset));
  params.set('limit', String(limit));
  return await apiClient.get(`/projects?${params}`) as Project[];
}

/**
 * Whether the current user is the current project's root — the owner, or an
 * admin with a project_members row.
 *
 * Reads the backend-computed capability flag `is_owner_like` first (one source
 * of truth: backend `is_project_root`, payload name `is_owner_like` — pinned
 * pair). The `owner_id === currentUser.user_id` fallback is rollout safety for
 * payloads that predate the flag; step 6 covers every path that feeds
 * `currentProject`. The payload carries capability flags, never the instance
 * role — a future moderator role changes ONE backend predicate, not TSX.
 */
import { useAppStore } from '../store/app-store';

export function useIsProjectOwner(): boolean {
  const project = useAppStore(s => s.currentProject);
  const currentUser = useAppStore(s => s.currentUser);
  return project?.is_owner_like
    ?? (!!project?.owner_id && !!currentUser && project.owner_id === currentUser.user_id);
}

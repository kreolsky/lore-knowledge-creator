/**
 * Connected-user chips for the active collab entity — one colored name tag per
 * other user. Color matches that user's gutter bar (userColor by user_id).
 */

import { useAppStore } from '../../store/app-store';
import { userColor } from '../../utils/user-color';
import { t } from '../../i18n';

export function CollabPresenceChips() {
  const collabUsers = useAppStore(s => s.collabUsers);
  const currentUserId = useAppStore(s => s.currentUser?.user_id);

  const others = collabUsers.filter(u => u.user_id !== currentUserId);
  if (others.length === 0) return null;

  return (
    <div className="absolute top-1 right-2 z-10 flex gap-1 pointer-events-none">
      {others.map(user => (
        <span
          key={user.user_id}
          className="px-1.5 py-0.5 text-xs leading-none text-white whitespace-nowrap pointer-events-auto"
          // WHY: chip color === gutter bar color === userColor(user_id). Why:
          // the user matches the margin bar to the name tag at a glance.
          style={{ background: userColor(user.user_id) }}
          title={`${t('collabPresenceEditing')}: ${user.name}`}
        >
          {user.name}
        </span>
      ))}
    </div>
  );
}

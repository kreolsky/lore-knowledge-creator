/** Admin: per-project user access management. */

import { useState } from 'react';
import { AccessLevel, User } from '../../types';
import { Dropdown, IconButton } from '../ui';
import { useTranslation } from '../../i18n';

interface ProjectMemberListProps {
  members: Record<string, string>;
  users: User[];
  ownerId: string | null;
  ownerName: string | null;
  projectId: string;
  onSetAccess: (projectId: string, userId: string, access: AccessLevel | null) => void;
  /** Resolves true on success — the no-jump order snapshot is dropped when false. */
  onTransferOwner: (projectId: string, userId: string) => Promise<boolean>;
}

export function ProjectMemberList({ members, users, ownerId, ownerName, projectId, onSetAccess, onTransferOwner }: ProjectMemberListProps) {
  const { t } = useTranslation();
  // No-jump rule: on an Owner pick, snapshot the
  // current rendered uid order with the target replaced by the previous
  // owner's uid, so after the reload the old owner takes the target's exact
  // slot — no row appears or disappears. Component state only: a remount
  // (collapse/re-expand) returns to the normal access-then-name sort, which is
  // an ordinary re-sort, not a jump.
  const [order, setOrder] = useState<string[] | null>(null);

  const accessOrder: Record<string, number> = { full: 0, commentator: 1, readonly: 2 };
  const memberEntries = Object.entries(members);
  const sortedBase = memberEntries
    .map(([uid, access]) => ({ user: users.find(u => u.user_id === uid), access }))
    .filter((m): m is { user: User; access: string } => !!m.user)
    // WHY: owner is never a member row — the owner renders as the pinned
    // first row instead, so excluding them here prevents double-display and
    // accidental role changes.
    .filter(m => m.user.user_id !== ownerId)
    .sort((a, b) => (accessOrder[a.access] - accessOrder[b.access]) || a.user.name.localeCompare(b.user.name));
  // Snapshot order first (uids no longer present dropped), uids new to the
  // snapshot appended in the normal sort order.
  const sorted = order
    ? [
        ...order
          .map(uid => sortedBase.find(m => m.user.user_id === uid))
          .filter((m): m is { user: User; access: string } => !!m),
        ...sortedBase.filter(m => !order.includes(m.user.user_id)),
      ]
    : sortedBase;

  const ownerUser = ownerId ? users.find(u => u.user_id === ownerId) : undefined;

  const pickOwner = async (userId: string) => {
    // An ownerless project has no previous owner to take the slot: the target
    // simply moves up into the pinned row.
    if (ownerId) setOrder(sorted.map(m => (m.user.user_id === userId ? ownerId : m.user.user_id)));
    const ok = await onTransferOwner(projectId, userId);
    // A failed transfer must not leave a phantom order (rows showing a
    // transfer that did not happen).
    if (!ok) setOrder(null);
  };

  const grantedIds = new Set(memberEntries.map(([uid]) => uid));
  const available = users.filter(u => u.user_id !== ownerId && !grantedIds.has(u.user_id));

  return (
    <div className="border-t border-border py-2 px-3.5 pl-10 pb-3">
      <p className="text-xs text-text-dim mb-2">
        {t('userAccess')}
      </p>
      <div className="flex flex-col gap-1">
        {/* Pinned owner row: the role cannot be changed or removed here —
            ownership moves only via the `Owner` option on a member row. */}
        <div className="flex items-center gap-2">
          <span className="flex-1 text-ui-base text-text overflow-hidden text-ellipsis whitespace-nowrap">
            {ownerUser?.name ?? ownerName ?? ''}
            {ownerUser && <span className="text-ui-xs text-text-dim font-normal ml-2">{ownerUser.email}</span>}
          </span>
          <Dropdown
            placement="bottom"
            align="right"
            title={t('ownerRole')}
            value="owner"
            disabled
            options={[{ value: 'owner', label: t('ownerRole') }]}
            onSelect={() => {}}
          />
          {/* IconButton-sm-sized spacer keeps the × column aligned. */}
          <span className="w-[22px] h-[22px] shrink-0" aria-hidden="true" />
        </div>
        {sorted.map(({ user, access }) => (
          <div key={user.user_id} className="flex items-center gap-2">
            <span className="flex-1 text-ui-base text-text overflow-hidden text-ellipsis whitespace-nowrap">
              {user.name}
              <span className="text-ui-xs text-text-dim font-normal ml-2">{user.email}</span>
            </span>
            <Dropdown
              placement="bottom"
              align="right"
              title={t('userAccess')}
              value={access}
              options={[
                { value: 'readonly', label: t('readOnly') },
                { value: 'commentator', label: t('commentator') },
                { value: 'full', label: t('fullAccess') },
                { value: 'owner', label: t('ownerRole') },
              ]}
              onSelect={v => {
                if (v === 'owner') void pickOwner(user.user_id);
                else onSetAccess(projectId, user.user_id, v as AccessLevel);
              }}
            />
            <IconButton
              size="sm"
              danger
              onClick={() => onSetAccess(projectId, user.user_id, null)}
              title="Remove access"
            >×</IconButton>
          </div>
        ))}
        {/* `flex` shrinks the popover root to the trigger, so the option panel
            (min-w-full of its root) matches the trigger, not the row. */}
        <div className="mt-1 flex">
          {/* Action picker, not a value: the trigger always reads the placeholder
              and every pick grants read-only (raise it in the row above). */}
          <Dropdown
            placement="bottom"
            triggerLabel={t('addUserPlaceholder')}
            title={t('addUserPlaceholder')}
            value=""
            disabled={!available.length}
            options={available.map(u => ({ value: u.user_id, label: u.name }))}
            onSelect={uid => onSetAccess(projectId, uid, 'readonly')}
          />
        </div>
      </div>
    </div>
  );
}

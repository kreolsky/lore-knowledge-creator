/** Admin/moderator: full user list — per-user card. Collapsed row = name ·
 * email | role | group: <name> | invited by: <name>, one line; the chevron
 * expands a panel UNDER the row (name / email / new password, one Save,
 * delete; admin additionally gets role + group selects saved through the
 * same PATCH — but never on the admin's OWN card: a self role change is
 * refused server-side, so the selects are not offered, like self-delete).
 * All mutation handlers stay in AdminPage; the list owns "which card is
 * open", each open card owns its draft.
 */

import { KeyboardEvent, useState } from 'react';
import { ChevronDown, ChevronRight, RefreshCw, Trash2, X } from 'lucide-react';
import { Button, Dropdown, FieldInput, IconButton } from '../ui';
import type { DropdownOption } from '../ui/Dropdown';
import { useTranslation } from '../../i18n';
import type { TranslationKey } from '../../i18n/en';
import { Role, User } from '../../types';
import { useArmedAction } from '../../hooks/useArmedAction';
import { useAppStore } from '../../store/app-store';
import { generatePassword } from '../../utils/generate-password';

export interface UserUpdates {
  name?: string;
  email?: string;
  password?: string;
  role?: Role;
  moderator_id?: string | null;
}

function UserDeleteButton({ onDelete }: { onDelete: () => void }) {
  const { t } = useTranslation();
  const deleteAction = useArmedAction();
  return (
    <IconButton
      danger
      filled={deleteAction.armed}
      onClick={() => deleteAction.handleClick(onDelete)}
      onMouseLeave={deleteAction.disarm}
      title={t('deleteUser')}
    >
      <Trash2 size={14} />
    </IconButton>
  );
}

type T = (k: TranslationKey) => string;

/** Option rows shared by the add form and the edit panel (same order, same labels). */
export function roleOptions(t: T): DropdownOption[] {
  return [
    { value: 'user', label: t('userRole') },
    { value: 'moderator', label: t('moderatorRole') },
    { value: 'admin', label: t('adminRole') },
  ];
}

/** "No group" first (value ''), then one row per moderator. */
export function groupOptions(t: T, moderators: User[]): DropdownOption[] {
  return [
    { value: '', label: t('noGroup') },
    ...moderators.map(m => ({ value: m.user_id, label: m.name })),
  ];
}

/** "| role" and, for a grouped user, "| <moderator name>" — one gray line
 * after the email; the group reads as a second chip, not a second line. */
/** One-line meta after the email: `| role | group: <name> | invited by: <name>`
 * — labelled, never wrapped onto a second row. */
function RoleChip({ role, groupName, inviterName }: { role: Role; groupName: string | null; inviterName: string | null }) {
  const { t } = useTranslation();
  const label = role === 'admin' ? t('adminRole') : role === 'moderator' ? t('moderatorRole') : t('userRole');
  // Each item is its own flex child so the parent's `gap-2` spaces email |
  // role | group | inviter identically (an inline `&nbsp;|` would add to the
  // gap on the first item only).
  const cls = 'text-xs text-text-dim font-normal whitespace-nowrap';
  return (
    <>
      <span className={`${cls} lowercase`}>| {label}</span>
      {groupName && <span className={cls}>| {t('group')}: {groupName}</span>}
      {inviterName && <span className={cls}>| {t('invitedBy')}: {inviterName}</span>}
    </>
  );
}

interface UserCardProps {
  user: User;
  currentUserId: string | undefined;
  expanded: boolean;
  onToggleExpand: () => void;
  onSave: (user: User, updates: UserUpdates) => void;
  onDelete: (userId: string) => void;
  /** Admin-only surfaces: role + group selects in the edit panel. */
  isAdmin: boolean;
  /** Moderator rows for the group select (admin only). */
  moderators: User[];
}

type UsersTabProps = Omit<UserCardProps, 'user' | 'expanded' | 'onToggleExpand'> & { users: User[] };

export function UsersTab({ users, ...cardProps }: UsersTabProps) {
  // One card open at a time (like AdminPage's expandedProject); opening
  // another card unmounts the first panel and its unsaved draft with it.
  const [expandedUserId, setExpandedUserId] = useState<string | null>(null);
  return (
    <div className="flex flex-col gap-0.5">
      {users.map(user => (
        <UserCard
          key={user.user_id}
          user={user}
          expanded={expandedUserId === user.user_id}
          onToggleExpand={() => setExpandedUserId(prev => prev === user.user_id ? null : user.user_id)}
          {...cardProps}
        />
      ))}
    </div>
  );
}

function UserCard({ user, currentUserId, expanded, onToggleExpand, onSave, onDelete, isAdmin, moderators }: UserCardProps) {
  const { t } = useTranslation();
  // Names come resolved from the server: a moderator's scoped list never
  // contains the moderator themself, so a client-side lookup rendered uids.
  const inviterName = user.created_by_name ?? null;
  const groupName = user.role === 'user' ? user.moderator_name ?? null : null;
  return (
    // Borderless pill with the references' hover tone (ListPill: hover =
    // surface2); an expanded card holds that tone while open.
    <section className={`hover:bg-surface2 transition-colors duration-150 ${expanded ? 'bg-surface2' : 'bg-surface'}`}>
      <div className="flex items-center py-2.5 px-3.5 gap-3">
        <IconButton size="sm" onClick={onToggleExpand} title={t('editNameEmail')}>
          {expanded
            ? <ChevronDown size={14} />
            : <ChevronRight size={14} />}
        </IconButton>
        <span className="flex-1 text-sm text-text font-medium flex gap-2 items-baseline min-w-0">
          {user.name}
          <span className="text-xs text-text-dim font-normal truncate">{user.email}</span>
          <RoleChip role={user.role} groupName={groupName} inviterName={inviterName} />
        </span>
      </div>

      {expanded && (
        <UserEditPanel
          user={user}
          canDelete={user.user_id !== currentUserId}
          isSelf={user.user_id === currentUserId}
          onSave={updates => { onSave(user, updates); onToggleExpand(); }}
          onCancel={onToggleExpand}
          onDelete={() => onDelete(user.user_id)}
          isAdmin={isAdmin}
          moderators={moderators}
        />
      )}
    </section>
  );
}

interface UserEditPanelProps {
  user: User;
  canDelete: boolean;
  /** The current user's own card: role/group selects stay hidden (the
   * server refuses self role changes — offer nothing that cannot save). */
  isSelf: boolean;
  onSave: (updates: UserUpdates) => void;
  onCancel: () => void;
  onDelete: () => void;
  isAdmin: boolean;
  moderators: User[];
}

/** Mounted only while the card is expanded, so the draft state resets on
 * every open and the password field never pre-fills.
 */
function UserEditPanel({ user, canDelete, isSelf, onSave, onCancel, onDelete, isAdmin, moderators }: UserEditPanelProps) {
  const { t } = useTranslation();
  const showToast = useAppStore(s => s.showToast);
  const [name, setName] = useState(user.name);
  const [email, setEmail] = useState(user.email);
  const [password, setPassword] = useState('');
  const [role, setRole] = useState<Role>(user.role);
  const [moderatorId, setModeratorId] = useState<string | null>(user.moderator_id ?? null);

  const commit = () => {
    const updates: UserUpdates = {};
    const trimmedName = name.trim();
    const trimmedEmail = email.trim();
    if (trimmedName && trimmedName !== user.name) updates.name = trimmedName;
    if (trimmedEmail && trimmedEmail !== user.email) updates.email = trimmedEmail;
    if (password) updates.password = password;
    // Admin fields ride the same PATCH, only when changed (a moderator's
    // body touching them is a 403 server-side — the selects are admin-only).
    if (isAdmin) {
      if (role !== user.role) updates.role = role;
      const currentGroup = user.moderator_id ?? null;
      if (role === 'user' ? moderatorId !== currentGroup : currentGroup !== null) {
        updates.moderator_id = role === 'user' ? moderatorId : null;
      }
    }
    onSave(updates);
  };

  const keys = (e: KeyboardEvent) => {
    if (e.key === 'Enter') commit();
    if (e.key === 'Escape') onCancel();
  };

  return (
    // Two rows, like the add form: identity (name / email / password) above,
    // role / group / Save / Cancel below, delete pushed to the far right.
    <div className="border-t border-border py-2 px-3.5 flex flex-col gap-2">
      <div className="flex items-center gap-2 flex-wrap">
        <FieldInput
          className="w-[140px]"
          value={name}
          onChange={e => setName(e.target.value)}
          placeholder={t('name')}
          autoComplete="off"
          autoFocus
          onKeyDown={keys}
        />
        <FieldInput
          type="email"
          className="w-[180px]"
          value={email}
          onChange={e => setEmail(e.target.value)}
          placeholder={t('email')}
          autoComplete="off"
          onKeyDown={keys}
        />
        <div className="flex gap-1">
          <FieldInput
            type="password"
            className="w-[140px]"
            value={password}
            onChange={e => setPassword(e.target.value)}
            placeholder={t('newPassword')}
            autoComplete="new-password"
            onKeyDown={keys}
          />
          <Button
            type="button"
            variant="primary"
            size="lg-square"
            title={t('generatePasswordTitle')}
            onClick={() => {
              const pwd = generatePassword();
              setPassword(pwd);
              navigator.clipboard.writeText(pwd);
              showToast(t('copied'), 'info');
            }}
          >
            <RefreshCw size={14} />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="lg-square"
            title={t('clearPassword')}
            disabled={!password}
            onClick={() => setPassword('')}
          >
            <X size={14} />
          </Button>
        </div>
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        {isAdmin && !isSelf && (
          <Dropdown
            size="lg"
            placement="bottom"
            title={t('role')}
            value={role}
            options={roleOptions(t)}
            onSelect={v => setRole(v as Role)}
          />
        )}
        {isAdmin && !isSelf && role === 'user' && (
          <Dropdown
            size="lg"
            placement="bottom"
            title={t('group')}
            value={moderatorId ?? ''}
            options={groupOptions(t, moderators)}
            onSelect={v => setModeratorId(v || null)}
          />
        )}
        <Button variant="primary" size="lg" onClick={commit}>{t('save')}</Button>
        <Button variant="ghost" size="lg" onClick={onCancel}>{t('cancel')}</Button>
        <span className="flex-1" />
        {canDelete && <UserDeleteButton onDelete={onDelete} />}
      </div>
    </div>
  );
}

/** Admin: user groups that models are granted to (SYSTEM: model-access).
 *
 * Two kinds from GET /api/admin/groups. Admin groups are stored: created here,
 * renamed, deleted (armed — the delete drops the group's memberships and
 * its grants), and their members edited as a checkbox list over every active
 * user, each toggle PUTting the full member list. Moderator groups are
 * VIRTUAL — derived from `users.moderator_id` — so they render read-only:
 * membership changes on the Users section, never here. Every card names the
 * models granted to the group; grants are edited on the Models section.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, Trash2 } from 'lucide-react';
import { Button, CenterHeading, FieldCheckbox, FieldInput, IconButton } from '../ui';
import {
  createAdminGroup, deleteAdminGroup, listAdminGroups, listAdminUsers,
  putAdminGroupMembers, renameAdminGroup, type AdminGroup,
} from '../../api/admin';
import type { User } from '../../types';
import { useAppStore } from '../../store/app-store';
import { useTranslation } from '../../i18n';
import type { TranslationKey } from '../../i18n/en';
import { useArmedAction } from '../../hooks/useArmedAction';
import { serverRefusalDetail } from '../../utils/server-refusal-detail';

type TFn = (key: TranslationKey, vars?: Record<string, string | number>) => string;

/** A group's display name: an admin group's own name; a moderator group is
 * "Moderator · <moderator name>" (the server sends only the name). */
export function groupLabel(group: AdminGroup, t: TFn): string {
  return group.kind === 'moderator' ? t('adminGroupModerator', { name: group.name }) : group.name;
}

function DeleteGroupButton({ onFire, disabled }: { onFire: () => void; disabled?: boolean }) {
  const { t } = useTranslation();
  const action = useArmedAction();
  return (
    <IconButton
      size="sm"
      danger
      filled={action.armed}
      onClick={() => action.handleClick(onFire)}
      onMouseLeave={action.disarm}
      disabled={disabled}
      title={t('delete')}
    >
      <Trash2 size={14} />
    </IconButton>
  );
}

/** Rename field + member checkboxes of one admin group. */
function AdminGroupEditor({ group, users, disabled, onRename, onMembers }: {
  group: AdminGroup;
  users: User[];
  disabled: boolean;
  onRename: (name: string) => void;
  onMembers: (userIds: string[]) => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(group.name);
  const [filter, setFilter] = useState('');
  const memberIds = group.members.map(m => m.user_id);
  const needle = filter.trim().toLowerCase();
  const visible = needle
    ? users.filter(u => u.name.toLowerCase().includes(needle) || u.email.toLowerCase().includes(needle))
    : users;
  const toggle = (uid: string, on: boolean) =>
    onMembers(on ? [...memberIds, uid] : memberIds.filter(id => id !== uid));
  return (
    <div className="flex flex-col gap-2">
      <div className="flex gap-2 items-center">
        <FieldInput
          className="flex-1"
          value={name}
          onChange={e => setName(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && name.trim()) onRename(name.trim()); }}
          disabled={disabled}
        />
        <Button
          size="lg"
          disabled={disabled || !name.trim() || name.trim() === group.name}
          onClick={() => onRename(name.trim())}
        >
          {t('save')}
        </Button>
      </div>
      <FieldInput
        className="w-full"
        placeholder={t('filterUsers')}
        value={filter}
        onChange={e => setFilter(e.target.value)}
      />
      <div className="flex flex-col gap-1.5 max-h-64 overflow-y-auto">
        {visible.map(u => (
          <FieldCheckbox
            key={u.user_id}
            checked={memberIds.includes(u.user_id)}
            onChange={on => toggle(u.user_id, on)}
            label={`${u.name} · ${u.email}`}
            disabled={disabled}
          />
        ))}
      </div>
    </div>
  );
}

export function GroupsSection() {
  const { t } = useTranslation();
  const [groups, setGroups] = useState<AdminGroup[] | null>(null);
  const [users, setUsers] = useState<User[]>([]);
  const [loadFailed, setLoadFailed] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ id: string; text: string } | null>(null);
  const [newName, setNewName] = useState('');
  const tRef = useRef(t);
  tRef.current = t;

  const reload = useCallback(async () => {
    try {
      const [g, u] = await Promise.all([listAdminGroups(), listAdminUsers()]);
      setGroups(g);
      setUsers(u);
      setLoadFailed(false);
    } catch {
      setLoadFailed(true);
      useAppStore.getState().showToast(tRef.current('adminListLoadFailed'), 'error');
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  /** Run one group mutation: busy flag, row-scoped refusal line, and the
   * server's group replacing the local one (null result = removed). */
  const mutate = async (id: string, call: () => Promise<AdminGroup | null>) => {
    setBusy(id);
    setMsg(null);
    try {
      const updated = await call();
      setGroups(prev => {
        if (!prev) return prev;
        if (updated === null) return prev.filter(g => g.id !== id);
        return prev.some(g => g.id === updated.id)
          ? prev.map(g => (g.id === updated.id ? updated : g))
          : [...prev, updated];
      });
      return true;
    } catch (err: unknown) {
      setMsg({ id, text: serverRefusalDetail(err) ?? t('adminGroupSaveFailed') });
      return false;
    } finally {
      setBusy(null);
    }
  };

  const create = async () => {
    const name = newName.trim();
    if (!name) return;
    if (await mutate('', () => createAdminGroup(name))) setNewName('');
  };

  // Admin groups first, then the moderator groups (the server's order).
  const adminGroups = (groups ?? []).filter(g => g.kind === 'admin');
  const moderatorGroups = (groups ?? []).filter(g => g.kind === 'moderator');

  const card = (group: AdminGroup) => {
    const open = expanded === group.id;
    const editable = group.kind === 'admin';
    return (
      <section
        key={group.id}
        data-group-id={group.id}
        className={`hover:bg-surface2 transition-colors duration-150 ${open ? 'bg-surface2' : 'bg-surface'}`}
      >
        <div className="py-2.5 px-3.5 flex items-center gap-3">
          <IconButton size="sm" onClick={() => setExpanded(open ? null : group.id)}>
            {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </IconButton>
          <span className="text-sm font-medium text-text truncate">{groupLabel(group, t)}</span>
          <span className="text-xs text-text-dim whitespace-nowrap">
            {t('adminGroupMemberCount', { count: group.members.length })}
          </span>
          <span className="flex-1" />
          {editable && (
            <DeleteGroupButton
              disabled={busy === group.id}
              onFire={() => void mutate(group.id, async () => { await deleteAdminGroup(group.id); return null; })}
            />
          )}
        </div>
        {open && (
          <div className="px-3.5 pb-3 pl-12 flex flex-col gap-2">
            <p className="text-xs text-text-dim">
              {t('adminGroupModels')}: {group.models.length ? group.models.join(', ') : t('adminGroupNoModels')}
            </p>
            {editable ? (
              <AdminGroupEditor
                group={group}
                users={users}
                disabled={busy === group.id}
                onRename={name => void mutate(group.id, () => renameAdminGroup(group.id, name))}
                onMembers={ids => void mutate(group.id, () => putAdminGroupMembers(group.id, ids))}
              />
            ) : (
              <>
                <p className="text-xs text-text-dim">{t('adminGroupModeratorHint')}</p>
                <p className="text-sm text-text">
                  {group.members.length ? group.members.map(m => m.name).join(', ') : t('adminGroupNoMembers')}
                </p>
              </>
            )}
          </div>
        )}
        {msg?.id === group.id && <p className="text-xs px-3.5 pb-2 text-red">{msg.text}</p>}
      </section>
    );
  };

  return (
    <>
      {/* Variant of the admin heading: mb-1 — the hint hugs the rule. */}
      <CenterHeading className="text-ui-md font-semibold text-text pb-1.5 border-b border-border mb-1">{t('adminGroups')}</CenterHeading>
      <p className="text-xs text-text-dim mb-3">{t('adminGroupsHint')}</p>
      {loadFailed && <p className="text-xs text-red mb-2">{t('adminListLoadFailed')}</p>}
      <div className="flex gap-2 items-center">
        <FieldInput
          className="flex-1"
          placeholder={t('adminGroupNewPlaceholder')}
          value={newName}
          onChange={e => setNewName(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') void create(); }}
          disabled={busy === ''}
        />
        <Button variant="primary" size="lg" disabled={busy === '' || !newName.trim()} onClick={() => void create()}>
          {t('adminGroupCreate')}
        </Button>
      </div>
      {msg?.id === '' && <p className="text-xs mt-1 text-red">{msg.text}</p>}
      <div className="flex flex-col gap-0.5 mt-3">
        {adminGroups.map(card)}
        {moderatorGroups.map(card)}
      </div>
    </>
  );
}

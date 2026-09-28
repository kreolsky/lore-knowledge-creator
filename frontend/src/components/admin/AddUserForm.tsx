/** Admin/moderator: add-user form with password generation + invite-link mint. */
// ARCH: Extracted from AdminPage — self-contained form with its own state.
// Role + moderator selects are ADMIN-only: a moderator's create is forced
// server-side to role='user' + own group, so the form hides both.

import type React from 'react';
import { useState } from 'react';
import { Link2, Plus, RefreshCw } from 'lucide-react';
import { Button, CenterHeading, Dropdown, FieldInput } from '../ui';
import { groupOptions, roleOptions } from './UsersTab';
import { useTranslation } from '../../i18n';
import { useAppStore } from '../../store/app-store';
import { Role, User } from '../../types';
import { generatePassword } from '../../utils/generate-password';

export interface AddUserData {
  name: string;
  email: string;
  password: string;
  role: Role;
  moderator_id: string | null;
}

interface AddUserFormProps {
  /** Rejects on a refused create (after toasting the reason); the form then keeps its draft. */
  onAdd: (data: AddUserData) => Promise<void>;
  /** Mints POST /api/invites and copies the URL to the clipboard. */
  onInvite: () => Promise<void>;
  isAdmin: boolean;
  /** Moderator rows for the group select (admin only). */
  moderators: User[];
}

export function AddUserForm({ onAdd, onInvite, isAdmin, moderators }: AddUserFormProps) {
  const { t } = useTranslation();
  const [newName, setNewName] = useState('');
  const [newEmail, setNewEmail] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [newRole, setNewRole] = useState<Role>('user');
  const [newModeratorId, setNewModeratorId] = useState<string | null>(null);
  const showToast = useAppStore(s => s.showToast);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await onAdd({
        name: newName, email: newEmail, password: newPassword,
        role: newRole, moderator_id: newRole === 'user' ? newModeratorId : null,
      });
      setNewName('');
      setNewEmail('');
      setNewPassword('');
      setNewRole('user');
      setNewModeratorId(null);
    } catch {
      // Refused: onAdd already toasted the server's reason — keep the draft
      // so the operator can correct it instead of retyping.
      return;
    }
  };

  return (
    <section className="mb-10">
      <div className="flex items-center gap-2 pb-1.5 border-b border-border mb-3">
        {/* Text-only variant: the flex row above carries the rule (heading + invite button). */}
        <CenterHeading className="text-ui-md font-semibold text-text">
          {t('addUser')}
        </CenterHeading>
        {/* Invite link: the no-password handoff — the inviter copies the URL
            and hands it over (both admin and moderator). */}
        <Button type="button" variant="ghost" size="sm" onClick={() => void onInvite()}>
          <Link2 size={14} />
          {t('inviteLink')}
        </Button>
      </div>
      {/* Two rows, always: identity (name / email / password) above, then
          role / group / Add — so the submit never sits between text fields. */}
      <form onSubmit={handleSubmit} className="flex flex-col gap-2 items-start">
        <div className="flex gap-2 flex-wrap items-end">
          <div>
            <label className="block text-ui-xs text-text-dim mb-1">{t('name')}</label>
            <FieldInput
              className="w-[140px]"
              value={newName}
              onChange={e => setNewName(e.target.value)}
              placeholder={t('displayNamePlaceholder')}
            />
          </div>
          <div>
            <label className="block text-ui-xs text-text-dim mb-1">{t('email')}</label>
            <FieldInput
              type="email"
              className="w-[180px]"
              value={newEmail}
              onChange={e => setNewEmail(e.target.value)}
              placeholder={t('emailPlaceholder')}
            />
          </div>
          <div>
            <label className="block text-ui-xs text-text-dim mb-1">{t('password')}</label>
            <div className="flex gap-1">
              <FieldInput
                type="text"
                className="w-[140px]"
                value={newPassword}
                onChange={e => setNewPassword(e.target.value)}
                placeholder={t('passwordPlaceholder')}
              />
              <Button
                type="button"
                variant="primary"
                size="lg-square"
                title={t('generatePasswordTitle')}
                onClick={() => {
                  const pwd = generatePassword();
                  setNewPassword(pwd);
                  navigator.clipboard.writeText(pwd);
                  showToast(t('copied'), 'info');
                }}
              >
                <RefreshCw size={14} />
              </Button>
            </div>
          </div>
        </div>
        <div className="flex gap-2 flex-wrap items-end">
          {isAdmin && (
            <div>
              <label className="block text-ui-xs text-text-dim mb-1">{t('role')}</label>
              <Dropdown
                size="lg"
                placement="bottom"
                title={t('role')}
                value={newRole}
                options={roleOptions(t)}
                onSelect={v => setNewRole(v as Role)}
              />
            </div>
          )}
          {isAdmin && newRole === 'user' && (
            <div>
              <label className="block text-ui-xs text-text-dim mb-1">{t('group')}</label>
              <Dropdown
                size="lg"
                placement="bottom"
                title={t('group')}
                value={newModeratorId ?? ''}
                options={groupOptions(t, moderators)}
                onSelect={v => setNewModeratorId(v || null)}
              />
            </div>
          )}
          <Button
            type="submit"
            variant="primary"
            size="lg"
            disabled={!newName || !newEmail || !newPassword}
          >
            <Plus size={14} />
            {t('add')}
          </Button>
        </div>
      </form>
    </section>
  );
}

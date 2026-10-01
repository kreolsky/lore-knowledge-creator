/** User cabinet: self-service profile management (name, email, password, PIN). Store slices: currentUser, setCurrentUser; ui-store (the aside width shared with the admin panel + project sidebar, panelWidths.left). */

import type React from 'react';
import { useState } from 'react';
import { User } from 'lucide-react';
import { useAppStore } from '../store/app-store';
import { useSectionAsideWidth } from '../hooks/useSectionAsideWidth';
import { useAppVersion } from '../hooks/useAppVersion';
import { Button, FieldInput } from '../components/ui';
import { SectionShell, SectionAsideRow } from '../components/SectionShell';
import { useTranslation } from '../i18n';

/** PATCH/POST to cabinet endpoints with error handling. Returns [data, errorMsg]. */
async function cabinetFetch(
  endpoint: string,
  body: Record<string, unknown>,
  method = 'PATCH',
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- callers access untyped JSON fields
): Promise<[any, string]> {
  const res = await fetch(`/api/cabinet/${endpoint}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(body),
  });
  if (res.status === 401) {
    window.location.href = '/';
    return [null, ''];
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const msg = data?.detail ?? `Error ${res.status}`;
    return [null, msg];
  }
  return [data, ''];
}

/** `null` = still loading, `''` = /api/health failed, anything else = the release tag. */
function versionState(version: string | null): 'loading' | 'error' | 'ok' {
  if (version === null) return 'loading';
  if (version === '') return 'error';
  return 'ok';
}

export function CabinetPage() {
  const currentUser = useAppStore(s => s.currentUser);
  const setCurrentUser = useAppStore(s => s.setCurrentUser);
  const { t } = useTranslation();

  // Aside width: ONE width with the admin panel and the project sidebar
  // (panelWidths.left, per-user global pref; see useSectionAsideWidth).
  const { initialAsideWidth, onAsideWidthChange } = useSectionAsideWidth();

  // ─── Release version ──────────────────────────────────────────────────────
  const version = useAppVersion();

  // ─── Name ─────────────────────────────────────────────────────────────────
  const [name, setName] = useState(currentUser?.name ?? '');
  const [nameMsg, setNameMsg] = useState('');
  const [nameSaving, setNameSaving] = useState(false);

  const handleSaveName = async () => {
    if (!name.trim() || name === currentUser?.name) return;
    setNameSaving(true);
    setNameMsg('');
    const [data, err] = await cabinetFetch('name', { name: name.trim() });
    setNameSaving(false);
    if (err) { setNameMsg(err); return; }
    setCurrentUser({ ...currentUser!, name: data.name });
    setNameMsg(t('saved'));
    setTimeout(() => setNameMsg(''), 2000);
  };

  // ─── Email ────────────────────────────────────────────────────────────────
  const [emailEditing, setEmailEditing] = useState(false);
  const [newEmail, setNewEmail] = useState('');
  const [emailPassword, setEmailPassword] = useState('');
  const [emailMsg, setEmailMsg] = useState('');
  const [emailSaving, setEmailSaving] = useState(false);

  const handleSaveEmail = async () => {
    if (!newEmail.trim() || !emailPassword) return;
    setEmailSaving(true);
    setEmailMsg('');
    const [data, err] = await cabinetFetch('email', {
      email: newEmail.trim(),
      current_password: emailPassword,
    });
    setEmailSaving(false);
    if (err) { setEmailMsg(err); return; }
    setCurrentUser({ ...currentUser!, email: data.email });
    setEmailEditing(false);
    setNewEmail('');
    setEmailPassword('');
    setEmailMsg(t('emailUpdated'));
    setTimeout(() => setEmailMsg(''), 2000);
  };

  // ─── Password ─────────────────────────────────────────────────────────────
  const [pwdEditing, setPwdEditing] = useState(false);
  const [currentPwd, setCurrentPwd] = useState('');
  const [newPwd, setNewPwd] = useState('');
  const [confirmPwd, setConfirmPwd] = useState('');
  const [pwdMsg, setPwdMsg] = useState('');
  const [pwdSaving, setPwdSaving] = useState(false);

  const handleSavePassword = async () => {
    if (!currentPwd || !newPwd) return;
    if (newPwd !== confirmPwd) { setPwdMsg(t('passwordsDoNotMatch')); return; }
    setPwdSaving(true);
    setPwdMsg('');
    const [, err] = await cabinetFetch('password', {
      current_password: currentPwd,
      new_password: newPwd,
    });
    setPwdSaving(false);
    if (err) { setPwdMsg(err); return; }
    setPwdEditing(false);
    setCurrentPwd('');
    setNewPwd('');
    setConfirmPwd('');
    setPwdMsg(t('passwordChanged'));
    setTimeout(() => setPwdMsg(''), 2000);
  };

  // ─── PIN ──────────────────────────────────────────────────────────────────
  const [pinValue, setPinValue] = useState('');
  const [pinMsg, setPinMsg] = useState('');
  const [pinSaving, setPinSaving] = useState(false);
  const hasPin = currentUser?.has_pin ?? false;

  const handleSetPin = async () => {
    if (pinValue.length !== 4) return;
    setPinSaving(true);
    setPinMsg('');
    const [data, err] = await cabinetFetch('pin', { pin: pinValue });
    setPinSaving(false);
    if (err) { setPinMsg(err); return; }
    setCurrentUser({ ...currentUser!, has_pin: data.has_pin });
    setPinValue('');
    setPinMsg(data.has_pin ? t('pinSet') : t('pinRemoved'));
    setTimeout(() => setPinMsg(''), 2000);
  };

  const handleRemovePin = async () => {
    setPinSaving(true);
    setPinMsg('');
    const [data, err] = await cabinetFetch('pin', { pin: null });
    setPinSaving(false);
    if (err) { setPinMsg(err); return; }
    setCurrentUser({ ...currentUser!, has_pin: data.has_pin });
    setPinValue('');
    setPinMsg(t('pinRemoved'));
    setTimeout(() => setPinMsg(''), 2000);
  };

  if (!currentUser) return null;

  // Lookup instead of a conditional chain in JSX (coding-constraints: no nested ternaries).
  const versionView = {
    loading: { text: '…', className: 'text-sm text-text-dim' },
    error: { text: t('appVersionUnavailable'), className: 'text-sm text-red' },
    ok: { text: version ?? '', className: 'text-sm font-mono' },
  }[versionState(version)];

  const savedText = t('saved');
  const emailUpdatedText = t('emailUpdated');
  const passwordChangedText = t('passwordChanged');

  // A tab-less section: the shell draws no top tabs — the UserControls user
  // button at the bottom of the bar is the active tab (see UserControls). The
  // aside lists the one selectable entry, the user's email, always selected.
  // The page <h1> is gone — the header shows "Profile".
  const renderAside = () => (
    <div className="px-1 pt-1">
      <SectionAsideRow label={currentUser.email} icon={<User size={14} />} selected />
    </div>
  );

  return (
    <SectionShell
      tabs={[]}
      renderAside={renderAside}
      initialAsideWidth={initialAsideWidth}
      onAsideWidthChange={onAsideWidthChange}
      renderCenter={() => (
      <div className="flex-1 overflow-y-auto">
      <div className="max-w-[640px] mx-auto py-10 px-6 text-text">

      {/* ─── Display Name ──────────────────────────────────────────── */}
      <div className="py-5 border-b border-border-soft">
        <label className="block text-xs text-text-dim mb-1.5">{t('displayName')}</label>
        <div className="flex gap-2 items-center">
          <FieldInput
            className="flex-1"
            value={name}
            onChange={e => setName(e.target.value)}
          />
          <Button
            variant="primary"
            size="lg"
            onClick={handleSaveName}
            disabled={nameSaving || !name.trim() || name === currentUser.name}
          >
            {t('save')}
          </Button>
        </div>
        {nameMsg && <p className={['text-xs mt-1', nameMsg === savedText ? 'text-green' : 'text-red'].join(' ')}>{nameMsg}</p>}
      </div>

      {/* ─── Email ─────────────────────────────────────────────────── */}
      <div className="py-5 border-b border-border-soft">
        <label className="block text-xs text-text-dim mb-1.5">{t('email')}</label>
        {!emailEditing ? (
          <div className="flex gap-2 items-center">
            <span className="flex-1 text-sm">{currentUser.email}</span>
            <Button size="lg" onClick={() => { setEmailEditing(true); setNewEmail(currentUser.email); }}>
              {t('change')}
            </Button>
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <FieldInput
              type="email"
              value={newEmail}
              onChange={e => setNewEmail(e.target.value)}
              placeholder={t('newEmail')}
              autoFocus
            />
            <FieldInput
              type="password"
              value={emailPassword}
              onChange={e => setEmailPassword(e.target.value)}
              placeholder={t('currentPassword')}
              autoComplete="current-password"
            />
            <div className="flex gap-2">
              <Button
                variant="primary"
                size="lg"
                onClick={handleSaveEmail}
                disabled={emailSaving || !newEmail.trim() || !emailPassword}
              >
                {t('save')}
              </Button>
              <Button size="lg" onClick={() => { setEmailEditing(false); setEmailMsg(''); }}>
                {t('cancel')}
              </Button>
            </div>
          </div>
        )}
        {emailMsg && <p className={['text-xs mt-1', emailMsg === emailUpdatedText ? 'text-green' : 'text-red'].join(' ')}>{emailMsg}</p>}
      </div>

      {/* ─── Password ──────────────────────────────────────────────── */}
      <div className="py-5 border-b border-border-soft">
        <label className="block text-xs text-text-dim mb-1.5">{t('password')}</label>
        {!pwdEditing ? (
          <Button size="lg" onClick={() => setPwdEditing(true)}>
            {t('changePassword')}
          </Button>
        ) : (
          <div className="flex flex-col gap-2">
            <FieldInput
              type="password"
              value={currentPwd}
              onChange={e => setCurrentPwd(e.target.value)}
              placeholder={t('currentPassword')}
              autoComplete="current-password"
              autoFocus
            />
            <FieldInput
              type="password"
              value={newPwd}
              onChange={e => setNewPwd(e.target.value)}
              placeholder={t('newPassword')}
              autoComplete="new-password"
            />
            <FieldInput
              type="password"
              value={confirmPwd}
              onChange={e => setConfirmPwd(e.target.value)}
              placeholder={t('confirmNewPassword')}
              autoComplete="new-password"
            />
            <div className="flex gap-2">
              <Button
                variant="primary"
                size="lg"
                onClick={handleSavePassword}
                disabled={pwdSaving || !currentPwd || !newPwd || !confirmPwd}
              >
                {t('save')}
              </Button>
              <Button size="lg" onClick={() => { setPwdEditing(false); setPwdMsg(''); }}>
                {t('cancel')}
              </Button>
            </div>
          </div>
        )}
        {pwdMsg && <p className={['text-xs mt-1', pwdMsg === passwordChangedText ? 'text-green' : 'text-red'].join(' ')}>{pwdMsg}</p>}
      </div>

      {/* ─── PIN ───────────────────────────────────────────────────── */}
      <div className="py-5">
        <label className="block text-xs text-text-dim mb-1.5">{t('quickLockPin')}</label>
        <p className="text-xs text-text-dim mb-2">
          {hasPin ? t('pinIsSet') : t('setPinDescription')}
        </p>
        <div className="flex gap-2 items-center">
          <FieldInput
            className="w-[120px]"
            value={pinValue}
            onChange={e => {
              const v = e.target.value.replace(/\D/g, '').slice(0, 4);
              setPinValue(v);
            }}
            placeholder={t('fourDigits')}
            inputMode="numeric"
            maxLength={4}
          />
          <Button
            variant="primary"
            size="lg"
            onClick={handleSetPin}
            disabled={pinSaving || pinValue.length !== 4}
          >
            {hasPin ? t('changePin') : t('setPin')}
          </Button>
          {hasPin && (
            <Button
              size="lg"
              onClick={handleRemovePin}
              disabled={pinSaving}
            >
              {t('remove')}
            </Button>
          )}
        </div>
        {pinMsg && <p className={['text-xs mt-1', pinMsg === t('pinRemoved') || pinMsg === t('pinSet') ? 'text-green' : 'text-red'].join(' ')}>{pinMsg}</p>}
      </div>

      <div className="py-5">
        <label className="block text-xs text-text-dim mb-1.5">{t('appVersion')}</label>
        <span className={versionView.className}>{versionView.text}</span>
      </div>
      </div>
      </div>
    )}
    />
  );
}

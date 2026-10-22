/**
 * Project access sections — the sharing surfaces of the settings panel:
 * the project's audiences (one multi-row list) and project members.
 * Split from ProjectSettingsPanel (voice/image preferences stay there).
 */
// ARCH: every gate here mirrors a backend guard — the multi-row editor on
// can_publish_project (admin; _guard_audiences 403s the rest), the own-group
// control on owner-with-own-group, members on the project root
// (is_project_root). The backend enforces each gate; the UI only shows what
// the caller may actually use.

import { useEffect, useMemo, useState } from 'react';
import { useAppStore } from '../store/app-store';
import { Button, IconButton, FieldInput, SectionHeader, Dropdown, SegmentedControl } from './ui';
import type { DropdownOption } from './ui/Dropdown';
import type { TranslationKey } from '../i18n/en';
import { useTranslation } from '../i18n';
import { useIsProjectOwner } from '../hooks/useIsProjectOwner';
import { X, Users, UsersRound } from 'lucide-react';
import {
  fetchGroupRoster, inviteMember, listMembers, patchMember, removeMember, cancelInvite,
} from '../api/access';
import { listAdminGroups, type AdminGroup } from '../api/admin';
import type { AccessLevel, Audience, GroupRosterEntry, Project, ProjectMember } from '../types';

const ACCESS_RANK: Record<AccessLevel, number> = { readonly: 1, commentator: 2, full: 3 };

/** The subject that opens a project to every logged-in user (models/auth wire
 * name); readonly|commentator only, written by an admin (operator ruling). */
const PUBLIC_SUBJECT = 'public';

const roleOptions = (t: (k: TranslationKey) => string): DropdownOption[] => [
  { value: 'readonly', label: t('accessRoleReadonly') },
  { value: 'commentator', label: t('accessRoleCommentator') },
  { value: 'full', label: t('accessRoleFull') },
];

// A 'public' row is readonly|commentator only — write-for-everyone is never
// offered (the backend _guard_audiences refuses 'full'; this hides the option).
const levelOptions = (t: (k: TranslationKey) => string, subject: string): DropdownOption[] =>
  subject === PUBLIC_SUBJECT
    ? roleOptions(t).filter(o => o.value !== 'full')
    : roleOptions(t);

// ─── Audiences: the multi-row group-access list ─────────────────────────────
// One wire field (audiences: the FULL list per PATCH — the diff lives
// server-side in _guard_audiences) with three writer faces:
//   admin            — edits every row: subject ('public' + every group from
//                      ONE listAdminGroups call) + level + ×, one trailing
//                      empty row ("No group") that adds a row on pick;
//   group owner      — keeps the 4-stop own-group control (it edits ONLY the
//                      own-subject row) and sees every FOREIGN row as a
//                      read-only note with × — a grant the owner may revoke is
//                      never hidden from them;
//   no-group owner   — the note list alone, no section while the list is empty.
// A non-owner non-admin sees no section. Every × is a plain row drop: the
// backend lets the owner remove ANY row (narrowing one's own project's
// audience never weakens security), incl. the admin-set 'public' row.
//
// INVARIANT(security): a 'public' row (an instance-wide grant) is written by
// an admin only — a non-admin is never offered "All users" as an option.
// Why: an instance-wide grant is an operator decision, and
// write-for-everyone is never offered; the backend guard enforces the levels
// (public: readonly|commentator, clamped client-side on a subject swap), this
// hides the trigger.

export function AudienceSection({
  project, onPatch, busy,
}: {
  project: Project;
  onPatch: (updates: Record<string, unknown>) => void;
  busy: boolean;
}) {
  const { t } = useTranslation();
  const currentUser = useAppStore(s => s.currentUser);
  const isOwner = useIsProjectOwner();
  const canPublish = !!currentUser?.can_publish_project;
  const groupSubject = !canPublish ? (currentUser?.group_subject ?? null) : null;
  const [groups, setGroups] = useState<AdminGroup[]>([]);

  useEffect(() => {
    if (!canPublish) return;
    let cancelled = false;
    listAdminGroups()
      .then(rows => { if (!cancelled) setGroups(rows); })
      .catch(() => {
        if (!cancelled) useAppStore.getState().showToast(t('failedToLoadGroups'), 'error');
      });
    return () => { cancelled = true; };
  }, [canPublish, t]);

  const audiences = project.audiences ?? [];

  if (!canPublish && !isOwner) return null;
  const foreignRows = groupSubject
    ? audiences.filter(a => a.subject !== groupSubject)
    : audiences;
  if (!canPublish && !groupSubject && foreignRows.length === 0) return null;

  const patchList = (rows: Audience[]) => onPatch({ audiences: rows });
  const dropRow = (subject: string) => patchList(audiences.filter(a => a.subject !== subject));

  const subjectLabel = (subject: string): string =>
    subject === PUBLIC_SUBJECT ? t('accessAllUsers') : subject;

  const removeButton = (subject: string) => (
    <span data-remove-subject={subject}>
      <IconButton size="sm" danger title={t('accessRemove')} onClick={() => dropRow(subject)}>
        <X size={13} />
      </IconButton>
    </span>
  );

  /** Subject options minus the subjects already used by OTHER rows (a
   * duplicate subject is a 422 server-side). `except` = a filled row's own
   * subject, always offered; undefined = the trailing empty row, which also
   * carries the "No group" no-op stop. */
  const subjectOptions = (except?: string): DropdownOption[] => {
    const used = new Set(audiences.filter(a => a.subject !== except).map(a => a.subject));
    return [
      ...(except === undefined ? [{ value: '', label: t('noGroup') }] : []),
      { value: PUBLIC_SUBJECT, label: t('accessAllUsers') },
      ...groups.map(g => ({ value: g.subject, label: g.name })),
    ].filter(o => o.value === except || !used.has(o.value));
  };

  // Re-pointing a row swaps its subject in place; 'public' at 'full' would be
  // a 422, so the level clamps down to 'readonly' before the PATCH.
  const replaceSubject = (a: Audience, subject: string) => patchList(audiences.map(x =>
    x.subject === a.subject
      ? { subject, access_level: subject === PUBLIC_SUBJECT && x.access_level === 'full' ? 'readonly' : x.access_level }
      : x));

  const setLevel = (subject: string, level: string) => patchList(audiences.map(x =>
    x.subject === subject ? { ...x, access_level: level as AccessLevel } : x));

  const ownRow = groupSubject ? audiences.find(a => a.subject === groupSubject) ?? null : null;
  const handleOwnLevel = (v: string) => {
    if (v === 'none') { dropRow(groupSubject!); return; }
    if (ownRow) setLevel(groupSubject!, v);
    else patchList([...audiences, { subject: groupSubject!, access_level: v as AccessLevel }]);
  };

  return (
    <>
      <SectionHeader
        icon={<UsersRound size={11} />}
        title={t('accessGroupAudienceTitle')}
        description={t('accessGroupAudienceDesc')}
      />
      <div className="px-3.5 flex flex-col gap-1.5">
        {canPublish ? (
          <>
            {audiences.map(a => (
              <div key={a.subject} className="flex items-center gap-2">
                <Dropdown
                  size="sm"
                  align="left"
                  placement="bottom"
                  value={a.subject}
                  options={subjectOptions(a.subject)}
                  onSelect={v => { if (v && v !== a.subject) replaceSubject(a, v); }}
                  disabled={busy}
                />
                <Dropdown
                  size="sm"
                  align="right"
                  placement="bottom"
                  value={a.access_level}
                  options={levelOptions(t, a.subject)}
                  onSelect={v => setLevel(a.subject, v)}
                  disabled={busy}
                />
                {removeButton(a.subject)}
              </div>
            ))}
            {/* The trailing empty row — picking a subject adds that row; a new
             * empty row appears under it once the PATCH lands in the store. */}
            <div className="flex items-center gap-2">
              <Dropdown
                size="sm"
                align="left"
                placement="bottom"
                value=""
                options={subjectOptions()}
                onSelect={v => { if (v) patchList([...audiences, { subject: v, access_level: 'readonly' }]); }}
                disabled={busy}
              />
            </div>
          </>
        ) : (
          <>
            {groupSubject && (
              /* self-start: the section column is a flex-col, whose stretch
               * would widen the content-sized inline-flex radiogroup to the
               * full tab width (the note rows below DO stretch — flex-1 fill). */
              <div className="self-start">
                <SegmentedControl
                  ariaLabel={t('accessGroupAudienceTitle')}
                  options={[
                    { value: 'none', label: t('accessGroupNone') },
                    ...roleOptions(t),
                  ]}
                  value={ownRow ? ownRow.access_level : 'none'}
                  onChange={handleOwnLevel}
                  disabled={busy}
                />
              </div>
            )}
            {/* Foreign rows — admin-set grants on this project, shown as notes
             * with × (the owner may revoke any of them server-side). */}
            {foreignRows.map(a => (
              <div key={a.subject} data-audience-note={a.subject} className="flex items-center gap-2">
                <span className="flex-1 text-ui-xs text-text-dim">
                  {subjectLabel(a.subject)}
                  {' · '}
                  {roleOptions(t).find(o => o.value === a.access_level)?.label ?? a.access_level}
                </span>
                {removeButton(a.subject)}
              </div>
            ))}
          </>
        )}
      </div>
    </>
  );
}

// ─── Members: project-wide collaborators (root-only) ───────────────────────
// Moved verbatim from the pre-split PeopleSection in AccessPanel — Settings is
// project-scoped, so this is the right home. The per-doc Access tab keeps only
// document overrides.
//
// A group-bound owner (group_subject, non-admin) invites ROSTER emails only:
// one filterable roster Dropdown replaces the email field, and the invite
// handler refuses non-roster emails before any request — the backend gate
// (require_group_scoped_invite) 403s them regardless; the picker and the gate
// agree on the same boundary. Everyone else keeps the silent free-text invite.
//
// The group floor: while the project carries an audience row for the owner's
// OWN group, every roster member already holds that row's level (effective
// access is the MAX of member row and audiences — get_project_access). An
// invite at or below that level would change nothing, so those levels are
// disabled with the reason, and a member row below it carries a "via group"
// badge with the level that actually applies.

export function MembersSection({ projectId }: { projectId: string }) {
  const project = useAppStore(s => s.currentProject);
  const currentUser = useAppStore(s => s.currentUser);
  const showToast = useAppStore(s => s.showToast);
  const { t } = useTranslation();

  const [members, setMembers] = useState<ProjectMember[]>([]);
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteRole, setInviteRole] = useState<AccessLevel>('readonly');
  const [busy, setBusy] = useState(false);
  const [roster, setRoster] = useState<GroupRosterEntry[]>([]);

  const isOwner = useIsProjectOwner();
  const groupSubject = !currentUser?.can_publish_project
    ? (currentUser?.group_subject ?? null) : null;

  useEffect(() => {
    if (!isOwner || !projectId) return;
    let cancelled = false;
    listMembers(projectId)
      .then(m => { if (!cancelled) setMembers(m); })
      .catch(() => { if (!cancelled) showToast(t('failedToLoadMembers')); });
    return () => { cancelled = true; };
  }, [isOwner, projectId, showToast, t]);

  useEffect(() => {
    if (!isOwner || !groupSubject || !projectId) return;
    let cancelled = false;
    fetchGroupRoster()
      .then(rows => { if (!cancelled) setRoster(rows); })
      .catch(() => { if (!cancelled) showToast(t('failedToLoadRoster')); });
    return () => { cancelled = true; };
  }, [isOwner, groupSubject, projectId, showToast, t]);

  // Accounts already in the project (member rows and pending invites) are not
  // offered — inviting them again would only hit the "already invited" toast.
  const rosterOptions = useMemo<DropdownOption[]>(() => {
    const taken = new Set(members.flatMap(m => [m.user_id, m.email.toLowerCase()]).filter(Boolean));
    return roster
      .filter(r => !taken.has(r.user_id) && !taken.has(r.email.toLowerCase()))
      .map(r => ({ value: r.email, label: r.name || r.email, note: r.email }));
  }, [roster, members]);
  const rosterIds = useMemo(() => new Set(roster.map(r => r.user_id)), [roster]);

  const groupFloor = groupSubject
    ? (project?.audiences ?? []).find(a => a.subject === groupSubject)?.access_level ?? null
    : null;
  const inviteOptions = roleOptions(t).map(o =>
    groupFloor && ACCESS_RANK[o.value as AccessLevel] <= ACCESS_RANK[groupFloor]
      ? { ...o, disabled: true, note: t('accessViaGroupNote') } : o);
  const effectiveInviteRole = (inviteOptions.find(o => o.value === inviteRole && !o.disabled)
    ?? inviteOptions.find(o => !o.disabled))?.value as AccessLevel | undefined;
  const viaGroupLevel = (m: ProjectMember): AccessLevel | null =>
    groupFloor && !m.pending && rosterIds.has(m.user_id)
      && ACCESS_RANK[groupFloor] > ACCESS_RANK[m.access_level] ? groupFloor : null;

  if (!isOwner) return null;

  const refreshMembers = () => listMembers(projectId).then(setMembers).catch(() => showToast(t('failedToLoadMembers')));

  const handleInvite = async () => {
    const email = inviteEmail.trim().toLowerCase();
    if (!email || !effectiveInviteRole) return;
    if (currentUser && email === currentUser.email.toLowerCase()) {
      showToast(t('accessCannotInviteSelf')); return;
    }
    if (groupSubject && !roster.some(r => r.email.toLowerCase() === email)) {
      showToast(t('accessInviteOutsideGroup')); return;
    }
    if (members.some(m => m.email.toLowerCase() === email)) {
      showToast(t('accessAlreadyInvited', { email })); return;
    }
    setBusy(true);
    try {
      await inviteMember(projectId, inviteEmail.trim(), effectiveInviteRole);
      setInviteEmail('');
      showToast(t('accessInviteSent'));
      refreshMembers();
    } catch {
      showToast(t('accessInviteError'));
    } finally {
      setBusy(false);
    }
  };

  const handlePatch = async (uid: string, role: AccessLevel) => {
    try { await patchMember(projectId, uid, role); refreshMembers(); }
    catch { showToast(t('failedToSaveSettings')); }
  };

  const handleRemove = async (uid: string) => {
    try { await removeMember(projectId, uid); refreshMembers(); }
    catch { showToast(t('failedToSaveSettings')); }
  };

  const handleCancelPending = async (email: string) => {
    try { await cancelInvite(projectId, email); refreshMembers(); }
    catch { showToast(t('failedToSaveSettings')); }
  };

  const ownerId = project?.owner_id;

  return (
    <>
      <SectionHeader
        icon={<Users size={11} />}
        title={t('settingsAccessTitle')}
        description={t('settingsAccessDesc')}
      />
      <div className="px-3.5">
        <div className="flex flex-col gap-1.5 mb-2">
          {members.length === 0 ? (
            <div className="text-ui-xs text-text-dim">{t('accessNoMembers')}</div>
          ) : members
            .filter(m => m.user_id !== ownerId)
            .map(m => (
            <div key={`${m.user_id}|${m.email}`} className="flex items-center gap-2">
              <span className={`flex-1 text-ui-sm overflow-hidden text-ellipsis whitespace-nowrap ${m.pending ? 'italic text-text-dim' : 'text-text'}`}>
                {m.name || m.email}
                {m.pending && <span className="ml-1 text-ui-2xs uppercase">[{t('accessPendingBadge')}]</span>}
                {viaGroupLevel(m) && (
                  <span data-via-group={m.user_id} className="ml-1.5 text-ui-2xs text-text-dim not-italic">
                    {t('accessViaGroupBadge', { level: roleOptions(t).find(o => o.value === viaGroupLevel(m))!.label })}
                  </span>
                )}
              </span>
              {!m.pending ? (
                <Dropdown
                  align="right"
                  placement="bottom"
                  value={m.access_level}
                  options={roleOptions(t)}
                  onSelect={v => handlePatch(m.user_id, v as AccessLevel)}
                />
              ) : (
                <span className="text-ui-xs text-text-dim w-[110px] text-center">{m.access_level}</span>
              )}
              {m.pending ? (
                <IconButton size="sm" danger onClick={() => handleCancelPending(m.email)} title={t('accessCancelPending')}>
                  <X size={13} />
                </IconButton>
              ) : (
                <IconButton size="sm" danger onClick={() => handleRemove(m.user_id)} title={t('accessRemove')}>
                  <X size={13} />
                </IconButton>
              )}
            </div>
          ))}
        </div>
        <div className="flex items-center gap-2">
          {groupSubject ? (
            <Dropdown
              size="lg"
              fullWidth
              align="left"
              placement="bottom"
              value={inviteEmail}
              options={rosterOptions}
              triggerLabel={rosterOptions.find(o => o.value === inviteEmail)?.label ?? t('accessGroupPickerLabel')}
              filterPlaceholder={t('accessInviteFilterPlaceholder')}
              onSelect={v => setInviteEmail(v)}
              disabled={busy}
            />
          ) : (
            <FieldInput
              type="email"
              placeholder={t('accessAddByEmail')}
              value={inviteEmail}
              onChange={e => setInviteEmail(e.target.value)}
              className="flex-1"
              disabled={busy}
            />
          )}
          <Dropdown
            size="lg"
            align="right"
            placement="bottom"
            value={effectiveInviteRole ?? inviteRole}
            options={inviteOptions}
            onSelect={v => setInviteRole(v as AccessLevel)}
            disabled={busy || !effectiveInviteRole}
          />
          <Button variant="primary" size="lg" onClick={handleInvite} disabled={busy || !inviteEmail.trim() || !effectiveInviteRole}>
            {t('accessInvite')}
          </Button>
        </div>
        {!effectiveInviteRole && (
          <div className="mt-1.5 text-ui-xs text-text-dim">{t('accessInviteCoveredByGroup')}</div>
        )}
      </div>
    </>
  );
}

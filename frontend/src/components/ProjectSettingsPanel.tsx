/**
 * Project-level settings panel: voice recording target, reference image previews,
 * public access, and project-wide members.
 */
// ARCH: Only visible to users with accessLevel === 'full'. Persists via PATCH /api/projects/{id}.
// ARCH: Settings owns every project-scoped preference (voice target, image previews,
// public access) plus project members; the per-doc Access tab stays scoped to the
// current document. Personas are `.lore/system` documents, surfaced via the above-chat picker.

import { useState, useRef, useCallback, useEffect } from 'react';
import { useAppStore } from '../store/app-store';
import { apiClient } from '../api/client';
import { ParentPickerPopup } from './ParentPickerPopup';
import { Button, IconButton, FieldRadio, FieldCheckbox, FieldInput, SectionHeader, Dropdown } from './ui';
import type { DropdownOption } from './ui/Dropdown';
import type { TranslationKey } from '../i18n/en';
import { useTranslation } from '../i18n';
import { useIsProjectOwner } from '../hooks/useIsProjectOwner';
import { X, Mic, ChevronRight, Users, Globe, Image } from 'lucide-react';
import {
  inviteMember, listMembers, patchMember, removeMember, cancelInvite,
} from '../api/access';
import type { AccessLevel, ProjectMember } from '../types';

const roleOptions = (t: (k: TranslationKey) => string): DropdownOption[] => [
  { value: 'readonly', label: t('accessRoleReadonly') },
  { value: 'commentator', label: t('accessRoleCommentator') },
  { value: 'full', label: t('accessRoleFull') },
];

type VoiceMode = 'current' | 'fixed';

export function ProjectSettingsPanel() {
  const project = useAppStore(s => s.currentProject);
  const documents = useAppStore(s => s.documents);
  const setCurrentProject = useAppStore(s => s.setCurrentProject);
  const showToast = useAppStore(s => s.showToast);
  const { t } = useTranslation();

  const [saving, setSaving] = useState<string | null>(null);

  const [voicePickerOpen, setVoicePickerOpen] = useState(false);
  const voiceAnchorRef = useRef<HTMLSpanElement>(null);

  const voiceMode: VoiceMode = project?.voice_recording_doc_id ? 'fixed' : 'current';
  const voiceTargetDoc = project?.voice_recording_doc_id
    ? documents.find(d => d.document_id === project.voice_recording_doc_id)
    : null;

  const patchProject = useCallback(async (updates: Record<string, unknown>) => {
    const proj = useAppStore.getState().currentProject;
    if (!proj) return;
    setSaving(Object.keys(updates)[0] ?? 'unknown');
    try {
      const result = await apiClient.patch(`/projects/${proj.project_id}`, updates);
      setCurrentProject({ ...useAppStore.getState().currentProject!, ...updates, ...result });
    } catch {
      showToast(t('failedToSaveSettings'));
    } finally {
      setSaving(null);
    }
  }, [setCurrentProject, showToast, t]);

  const handleVoiceModeChange = (mode: VoiceMode) => {
    if (mode === 'current') {
      patchProject({ voice_recording_doc_id: null });
    } else if (!project?.voice_recording_doc_id) {
      setVoicePickerOpen(true);
    }
  };

  const handleVoiceDocSelect = (docId: string | null) => {
    setVoicePickerOpen(false);
    if (docId) {
      patchProject({ voice_recording_doc_id: docId });
    }
  };

  const handleClearVoiceTarget = () => {
    patchProject({ voice_recording_doc_id: null });
  };

  if (!project) return null;

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-auto py-1 px-1.5">
        <SectionHeader
          first
          icon={<Mic size={11} />}
          title={t('settingsVoiceTitle')}
          description={t('settingsVoiceDesc')}
        />
        <div className="px-3.5">
          <FieldRadio
            name="voiceMode"
            checked={voiceMode === 'current'}
            onChange={() => handleVoiceModeChange('current')}
            label={t('settingsVoiceCurrent')}
            description={t('settingsVoiceCurrentDesc')}
          />

          <span ref={voiceAnchorRef}>
            <FieldRadio
              name="voiceMode"
              checked={voiceMode === 'fixed'}
              onChange={() => handleVoiceModeChange('fixed')}
              label={t('settingsVoiceFixed')}
              description={t('settingsVoiceFixedDesc')}
            />
          </span>

          <div className={`ml-5 mt-1.5 flex items-center gap-2${voiceMode === 'fixed' ? '' : ' hidden'}`}>
            {voiceTargetDoc ? (
              <>
                <span className="text-sm text-text bg-surface3 px-2 py-0.5 overflow-hidden text-ellipsis whitespace-nowrap max-w-[200px]">
                  {voiceTargetDoc.title}
                </span>
                <span>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => setVoicePickerOpen(true)}
                    disabled={saving === 'voice_recording_doc_id'}
                  >
                    {t('settingsChange')}
                  </Button>
                </span>
                <IconButton
                  size="sm"
                  onClick={handleClearVoiceTarget}
                  title={t('settingsClear')}
                >
                  <X size={13} />
                </IconButton>
              </>
            ) : (
              <span>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setVoicePickerOpen(true)}
                  disabled={saving === 'voice_recording_doc_id'}
                >
                  <ChevronRight size={13} />
                  {t('settingsSelectDocument')}
                </Button>
              </span>
            )}
          </div>
        </div>

        <SectionHeader
          icon={<Image size={11} />}
          title={t('settingsImagePreviewTitle')}
          description={t('settingsImagePreviewDesc')}
        />
        <div className="px-3.5">
          <FieldCheckbox
            checked={project.ref_image_preview ?? true}
            onChange={v => patchProject({ ref_image_preview: v })}
            label={t('settingsImagePreviewLabel')}
            disabled={saving === 'ref_image_preview'}
          />
        </div>

        <PublicAccessSection
          isPublic={!!project.is_public}
          onToggle={v => patchProject({ is_public: v })}
        />

        <MembersSection projectId={project.project_id} />
      </div>

      {voicePickerOpen && voiceAnchorRef.current && (
        <ParentPickerPopup
          currentDocumentId={project.voice_recording_doc_id ?? undefined}
          onMoved={handleVoiceDocSelect}
          anchorRect={voiceAnchorRef.current.getBoundingClientRect()}
          onClose={() => setVoicePickerOpen(false)}
          above
        />
      )}
    </div>
  );
}

// ─── Public access: project-wide read-only broadcast (root-only) ───────────
// INVARIANT(security): only a project root (owner, or admin with a member row)
// sees/toggles is_public.
// Why: is_public grants read-only access to every logged-in user — a project-wide
// decision that must not be controllable by full-but-not-root collaborators
// (matches the backend root gate in routes/projects.py::patch_project).

function PublicAccessSection({
  isPublic, onToggle,
}: {
  isPublic: boolean; onToggle: (v: boolean) => void;
}) {
  const { t } = useTranslation();
  const isOwner = useIsProjectOwner();
  if (!isOwner) return null;

  return (
    <>
      <SectionHeader icon={<Globe size={11} />} title={t('accessPublicProject')} />
      <div className="px-3.5">
        <FieldCheckbox
          checked={isPublic}
          onChange={onToggle}
          label={t('accessPublicProjectDesc')}
        />
      </div>
    </>
  );
}

// ─── Members: project-wide collaborators (root-only) ───────────────────────
// Moved verbatim from the pre-split PeopleSection in AccessPanel — Settings is
// project-scoped, so this is the right home. The per-doc Access tab keeps only
// document overrides.

function MembersSection({ projectId }: { projectId: string }) {
  const project = useAppStore(s => s.currentProject);
  const currentUser = useAppStore(s => s.currentUser);
  const showToast = useAppStore(s => s.showToast);
  const { t } = useTranslation();

  const [members, setMembers] = useState<ProjectMember[]>([]);
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteRole, setInviteRole] = useState<AccessLevel>('readonly');
  const [busy, setBusy] = useState(false);

  const isOwner = useIsProjectOwner();

  useEffect(() => {
    if (!isOwner || !projectId) return;
    let cancelled = false;
    listMembers(projectId)
      .then(m => { if (!cancelled) setMembers(m); })
      .catch(() => { if (!cancelled) showToast(t('failedToLoadMembers')); });
    return () => { cancelled = true; };
  }, [isOwner, projectId, showToast, t]);

  if (!isOwner) return null;

  const refreshMembers = () => listMembers(projectId).then(setMembers).catch(() => showToast(t('failedToLoadMembers')));

  const handleInvite = async () => {
    const email = inviteEmail.trim().toLowerCase();
    if (!email) return;
    if (currentUser && email === currentUser.email.toLowerCase()) {
      showToast(t('accessCannotInviteSelf')); return;
    }
    if (members.some(m => m.email.toLowerCase() === email)) {
      showToast(t('accessAlreadyInvited', { email })); return;
    }
    setBusy(true);
    try {
      await inviteMember(projectId, inviteEmail.trim(), inviteRole);
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
          <FieldInput
            type="email"
            placeholder={t('accessAddByEmail')}
            value={inviteEmail}
            onChange={e => setInviteEmail(e.target.value)}
            className="flex-1"
            disabled={busy}
          />
          <Dropdown
            size="lg"
            align="right"
            placement="bottom"
            value={inviteRole}
            options={roleOptions(t)}
            onSelect={v => setInviteRole(v as AccessLevel)}
            disabled={busy}
          />
          <Button variant="primary" size="lg" onClick={handleInvite} disabled={busy || !inviteEmail.trim()}>
            {t('accessInvite')}
          </Button>
        </div>
      </div>
    </>
  );
}

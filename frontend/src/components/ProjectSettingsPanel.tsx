/**
 * Project-level settings panel: voice recording target and reference image
 * previews. The sharing surfaces (audiences, members) live in
 * ProjectAccessSections.
 */
// ARCH: Only visible to users with accessLevel === 'full'. Persists via PATCH /api/projects/{id}.
// ARCH: Settings owns every project-scoped preference (voice target, image
// previews); the access sections (audiences/members) are split into
// ProjectAccessSections and share this panel's patchProject. Personas are
// `.lore/system` documents, surfaced via the above-chat picker.

import { useState, useRef, useCallback } from 'react';
import { useAppStore } from '../store/app-store';
import { apiClient } from '../api/client';
import { ParentPickerPopup } from './ParentPickerPopup';
import { Button, IconButton, FieldRadio, FieldCheckbox, SectionHeader } from './ui';
import { useTranslation } from '../i18n';
import { X, Mic, ChevronRight, Image } from 'lucide-react';
import { AudienceSection, MembersSection } from './ProjectAccessSections';

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

        <AudienceSection
          project={project}
          onPatch={patchProject}
          busy={saving !== null}
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

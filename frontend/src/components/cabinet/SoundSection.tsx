/** Cabinet section: pick the agent notification sound — a bundled preset or the user's own uploaded
 * file — and preview it. One sound serves every cue (agent finished, confirmation needed).
 * Store slices: ui-store (soundPreset, setSoundPreset), app-store (showToast). The on/off toggle is per project (project settings). */

import { useEffect, useRef, useState } from 'react';
import { Play, Trash2, Upload } from 'lucide-react';
import { useUIStore } from '../../store/ui-store';
import { useAppStore } from '../../store/app-store';
import { DEFAULT_SOUND_PRESET, playSoundPreset, SOUND_PRESETS, type SoundPresetId } from '../../utils/sound-presets';
import { loadCustomSound, removeCustomSound, saveCustomSound, type CustomSoundError } from '../../utils/custom-sound';
import { Button, CenterHeading, FieldRadio, IconButton } from '../ui';
import { useTranslation, type TranslationKey } from '../../i18n';

const PRESET_LABELS: Record<SoundPresetId, TranslationKey> = {
  chime: 'soundPresetChime',
  pop: 'soundPresetPop',
  bell: 'soundPresetBell',
  soft: 'soundPresetSoft',
};

const UPLOAD_ERRORS: Record<CustomSoundError, TranslationKey> = {
  notAudio: 'soundUploadNotAudio',
  tooLarge: 'soundUploadTooLarge',
  readFailed: 'soundUploadReadFailed',
};

/** WHY: an imperative picker — the file input never renders, so no raw <input> in JSX. */
function pickAudioFile(onFile: (file: File) => void): void {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = 'audio/*';
  input.onchange = () => {
    const file = input.files?.[0];
    if (file) onFile(file);
  };
  input.click();
}

/** `undefined` = still loading, `null` = no file stored, `false` = the load failed. */
type CustomState = string | null | undefined | false;

export function SoundSection() {
  const { t } = useTranslation();
  const tRef = useRef(t);
  tRef.current = t;
  const soundPreset = useUIStore(s => s.soundPreset);
  const setSoundPreset = useUIStore(s => s.setSoundPreset);
  const showToast = useAppStore(s => s.showToast);
  const [customName, setCustomName] = useState<CustomState>(undefined);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    loadCustomSound().then(
      sound => setCustomName(sound?.name ?? null),
      () => {
        setCustomName(false);
        useAppStore.getState().showToast(tRef.current('soundCustomLoadFailed'), 'error');
      },
    );
  }, []);

  const handleUpload = () => pickAudioFile(async file => {
    setBusy(true);
    try {
      const error = await saveCustomSound(file);
      if (error) { showToast(t(UPLOAD_ERRORS[error]), 'error'); return; }
      setCustomName(file.name);
      setSoundPreset('custom');
    } catch {
      showToast(t('soundUploadFailed'), 'error');
    } finally {
      setBusy(false);
    }
  });

  const handleRemove = async () => {
    setBusy(true);
    try {
      await removeCustomSound();
      setCustomName(null);
      if (soundPreset === 'custom') setSoundPreset(DEFAULT_SOUND_PRESET);
    } catch {
      showToast(t('soundRemoveFailed'), 'error');
    } finally {
      setBusy(false);
    }
  };

  const hasCustom = typeof customName === 'string';
  const customDescription = customName === undefined ? t('loading')
    : customName === false ? t('soundCustomLoadFailed')
    : customName ?? t('soundCustomNone');

  return (
    <div>
      <CenterHeading>{t('cabinetSectionSound')}</CenterHeading>
      <p className="text-xs text-text-dim mb-4">{t('soundDescription')}</p>
      <div className="grid grid-cols-[minmax(0,1fr)_28px] gap-x-3 items-center">
        {SOUND_PRESETS.map(id => (
          <div key={id} className="contents">
            <FieldRadio
              name="sound-preset"
              checked={soundPreset === id}
              onChange={() => setSoundPreset(id)}
              label={t(PRESET_LABELS[id])}
            />
            <IconButton onClick={() => playSoundPreset(id)} title={t('soundPreview')} aria-label={t('soundPreview')}>
              <Play size={14} />
            </IconButton>
          </div>
        ))}
        <div className={hasCustom ? '' : 'opacity-60 pointer-events-none'}>
          <FieldRadio
            name="sound-preset"
            checked={soundPreset === 'custom'}
            onChange={() => setSoundPreset('custom')}
            label={hasCustom ? (
              // Same "primary | dim secondary" row as the project card's owner | members meta.
              <span className="inline-flex items-center gap-1 min-w-0">
                <span>{t('soundPresetCustom')}</span>
                <span aria-hidden="true" className="text-text-dim">|</span>
                <span className="text-text-dim truncate">{customName}</span>
              </span>
            ) : t('soundPresetCustom')}
            description={hasCustom ? undefined : customDescription}
          />
        </div>
        {hasCustom ? (
          <IconButton onClick={() => playSoundPreset('custom')} title={t('soundPreview')} aria-label={t('soundPreview')}>
            <Play size={14} />
          </IconButton>
        ) : <span />}
      </div>
      <div className="flex gap-2 mt-3 flex-wrap">
        <Button variant="panel" onClick={handleUpload} disabled={busy}><Upload size={14} />{t('soundUpload')}</Button>
        {hasCustom && (
          <Button variant="panel" onClick={handleRemove} disabled={busy}><Trash2 size={14} />{t('soundRemove')}</Button>
        )}
      </div>
      <p className="text-xs text-text-muted mt-2">{t('soundUploadHint')}</p>
    </div>
  );
}

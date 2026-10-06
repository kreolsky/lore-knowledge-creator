/** Shared mic button — recording/transcribing states. Replaces the duplicated
 * mic markup in both AI and notes composers. */
// SYSTEM: chat-composer — shared composer mic control

import { Mic, Loader2 } from 'lucide-react';
import { IconButton } from '../../ui';
import { useTranslation } from '../../../i18n';

interface Props {
  recording: boolean;
  transcribing: boolean;
  onToggleRecording: () => void;
  /** Idle fill one tone darker than the composer, so the icon doesn't melt into it. */
  filled?: boolean;
}

export function MicButton({ recording, transcribing, onToggleRecording, filled = false }: Props) {
  const { t } = useTranslation();
  return (
    <div className="mic-btn-wrap">
      <IconButton
        className={`${recording ? 'mic-recording' : transcribing ? 'mic-transcribing' : ''}${filled ? ' mic-filled' : ''}`}
        title={recording ? t('stopRecording') : transcribing ? t('transcribing') : t('voiceInput')}
        onClick={onToggleRecording}
        disabled={transcribing}
      >
        {transcribing ? (
          <Loader2 size={16} className="animate-spin" />
        ) : (
          <Mic size={16} />
        )}
      </IconButton>
    </div>
  );
}

/**
 * Simple voice recording hook — record audio, transcribe, return text via callback.
 *
 * INVARIANT: MediaRecorder config MUST mirror useAudioRecorder (opus codec +
 *   start(1000) + size-guarded chunks). Why: without timeslice/codec some browsers
 *   deliver an empty/degraded stream → Whisper returns "" → onTranscribed("")
 *   silently no-ops. This divergence was the root cause of the chat/note
 *   voice-input regression (header REC used the other hook and kept working).
 * cancelRecording discards the take (Escape in the composer): the recorder stops and
 *   the mic is released, but nothing is transcribed and no toast is shown.
 * INVARIANT: every failure path surfaces a toast (no silent degradation per
 *   CLAUDE.md). Why: the original bug went undiagnosed because non-2xx responses,
 *   network errors, and empty transcriptions were swallowed by `catch {}`/`if (resp.ok)`
 *   with no UI feedback. credentials:'include' mirrors apiClient for cookie auth.
 */

import { useState, useRef, useCallback } from 'react';
import { useAppStore } from '../store/app-store';
import { useTranslation } from '../i18n';

export function useSimpleVoiceRecording(onTranscribed: (text: string) => void) {
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const cancelledRef = useRef(false);
  const { t } = useTranslation();

  const toggleRecording = useCallback(async () => {
    if (recording) {
      mediaRecorderRef.current?.stop();
      setRecording(false);
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream, { mimeType: 'audio/webm;codecs=opus' });
      chunksRef.current = [];
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      recorder.onstop = async () => {
        stream.getTracks().forEach(tr => tr.stop());
        if (cancelledRef.current) {
          cancelledRef.current = false;
          chunksRef.current = [];
          return;
        }
        const blob = new Blob(chunksRef.current, { type: 'audio/webm' });
        chunksRef.current = [];
        if (blob.size === 0) {
          useAppStore.getState().showToast(t('voiceTranscriptionEmpty'), 'error');
          return;
        }
        setTranscribing(true);
        try {
          const form = new FormData();
          form.append('file', blob, 'recording.webm');
          const resp = await fetch('/api/chat/transcribe', { method: 'POST', body: form, credentials: 'include' });
          if (!resp.ok) {
            useAppStore.getState().showToast(t('voiceTranscriptionFailed'), 'error');
            return;
          }
          const { text } = await resp.json();
          const txt = (text ?? '').trim();
          if (!txt) {
            useAppStore.getState().showToast(t('voiceTranscriptionEmpty'), 'error');
            return;
          }
          onTranscribed(txt);
        } catch (e) {
          console.error('Transcription failed:', e);
          useAppStore.getState().showToast(t('voiceTranscriptionFailed'), 'error');
        } finally {
          setTranscribing(false);
        }
      };
      mediaRecorderRef.current = recorder;
      recorder.start(1000);
      setRecording(true);
    } catch (e) {
      console.error('Mic access denied:', e);
      useAppStore.getState().showToast(t('micAccessDenied'), 'error');
    }
  }, [recording, onTranscribed, t]);

  const cancelRecording = useCallback(() => {
    if (!mediaRecorderRef.current || mediaRecorderRef.current.state === 'inactive') return;
    cancelledRef.current = true;
    mediaRecorderRef.current.stop();
    setRecording(false);
  }, []);

  return { recording, transcribing, toggleRecording, cancelRecording };
}

/**
 * Simple voice recording hook — record audio, transcribe, return text via callback.
 *
 * The MediaRecorder comes from startOpusRecorder (useAudioRecorder) — one config for every mic.
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
import { startOpusRecorder } from './useAudioRecorder';

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
      chunksRef.current = [];
      const recorder = startOpusRecorder(stream, chunk => chunksRef.current.push(chunk));
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

/**
 * MediaRecorder lifecycle hook — captures WebM audio, calls onComplete with blob.
 *
 * ARCH: Module-level state (not React refs) — recording survives React
 * unmount/remount during document navigation. Same pattern as the editor/active-editor slots.
 *
 * Recording lifecycle:
 *   emit('start-recording') → startRecording() → getUserMedia → MediaRecorder
 *   → setRecording({...}) in store → header button shows red
 *
 *   emit('stop-recording') → stopRecording() → recorder.stop() → setRecording(null)
 *   → header button returns to idle → recorder.onstop fires → onComplete(file, ctx)
 *
 *   onComplete is handleRecordingComplete (ProjectPage) which routes to either:
 *   - handleVoiceRecording (sync STT for inline widget) or
 *   - async reference upload (for header-button recordings)
 */

import { useCallback, useEffect } from 'react';
import { useAppStore } from '../store/app-store';
import { t } from '../i18n';
import { useEvent } from './useEvent';
import { setVoiceCancelled, peekVoiceCancelled } from '../components/editor/voice-cancel-flag';
import { appendChunk, createSession, deleteSession, finalizeSession } from '../utils/recording-cache';

interface RecordingContext {
  projectId: string;
  documentId: string;
  /** The cache session id of THIS take — the upload idempotency key. Captured
   * per recorder at start and handed to the completion consumer, never read
   * from a global: a take started before an older upload's handler ran must
   * not upload under the wrong key (mirrors the finalize rule in onstop). */
  sessionId: string;
}

// Module-level — survives React lifecycle (document switches, panel tab changes)
let _recorder: MediaRecorder | null = null;
let _chunks: Blob[] = [];
let _context: RecordingContext | null = null;
let _onComplete: ((file: File, ctx: RecordingContext) => void) | null = null;

function newSessionId(): string {
  return typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `rec-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/**
 * Manages MediaRecorder lifecycle at ProjectPage level so recording
 * survives right-panel tab switches and document navigation.
 *
 * Captures document context at start time — upload always targets the
 * original document regardless of subsequent navigation.
 */
export function useAudioRecorder(
  onComplete: (file: File, context: RecordingContext) => void,
) {
  // Keep callback ref fresh without React ref (module-level)
  _onComplete = onComplete;

  const stopRecording = useCallback(() => {
    if (_recorder && _recorder.state !== 'inactive') {
      _recorder.stop();
    }
    _recorder = null;
    useAppStore.getState().setRecording(null);
  }, []);

  const startRecording = useCallback(async () => {
    if (_recorder) return;

    const { currentProject, currentDocument, accessLevel, documents } = useAppStore.getState();
    if (!currentProject || !currentDocument || accessLevel !== 'full') return;

    const voiceTargetId = currentProject.voice_recording_doc_id;
    const useCustomTarget = !!voiceTargetId
      && documents.some(d => d.document_id === voiceTargetId);
    const targetDocId = useCustomTarget ? voiceTargetId! : currentDocument.document_id;
    const targetDoc = useCustomTarget
      ? documents.find(d => d.document_id === voiceTargetId)
      : currentDocument;

    const sessionId = newSessionId();
    _context = {
      projectId: currentProject.project_id,
      documentId: targetDocId,
      sessionId,
    };

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream, { mimeType: 'audio/webm;codecs=opus' });
      _chunks = [];

      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) {
          _chunks.push(e.data);
          // Fire-and-forget: the cache's queue serializes writes per session.
          void appendChunk(sessionId, e.data);
        }
      };

      recorder.onstop = () => {
        stream.getTracks().forEach(t => t.stop());
        const blob = new Blob(_chunks, { type: 'audio/webm' });
        const now = new Date();
        const ts = `${now.getFullYear()}-${(now.getMonth() + 1).toString().padStart(2, '0')}-${now.getDate().toString().padStart(2, '0')} ${now.getHours().toString().padStart(2, '0')}:${now.getMinutes().toString().padStart(2, '0')}`;
        const file = new File([blob], `voice-${ts}.webm`, { type: 'audio/webm' });
        // Cache handoff: Escape = user discard (drop now — the consuming
        // isVoiceCancelled() read still happens downstream in useVoiceInput);
        // otherwise the take is complete-but-unsent → pending-upload until a 2xx.
        // The session id is captured per recorder: a take started before an older
        // upload finished must never finalize under the wrong id.
        if (peekVoiceCancelled()) void deleteSession(sessionId);
        else void finalizeSession(sessionId, file.name);
        if (_context && _onComplete) {
          _onComplete(file, _context);
        }
      };

      recorder.start(1000);
      _recorder = recorder;
      // Only after start() succeeded, in the same synchronous block: a
      // getUserMedia/MediaRecorder failure leaves no empty record, and the first
      // ondataavailable (~1s, event loop) can never overtake the queued create.
      void createSession(sessionId, currentProject.project_id, targetDocId);

      useAppStore.getState().setRecording({
        startedAt: Date.now(),
        targetDocumentId: targetDocId,
        targetDocumentTitle: targetDoc?.title ?? currentDocument.title,
      });
    } catch (err) {
      console.error('Failed to start recording', err);
      _context = null;
      const denied = err instanceof DOMException && err.name === 'NotAllowedError';
      useAppStore.getState().showToast(t(denied ? 'micAccessDenied' : 'recordingStartFailed'), 'error');
    }
  }, []);

  useEvent('start-recording', startRecording);
  useEvent('stop-recording', stopRecording);

  // Escape cancels recording from top bar (mirrors voice-widget Escape behavior).
  // Only fires when no interactive element has focus — avoids stealing Escape
  // from sidebar rename, modal dismiss, etc.
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || !_recorder) return;
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || (e.target as HTMLElement)?.isContentEditable) return;
      e.preventDefault();
      setVoiceCancelled(true);
      stopRecording();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [stopRecording]);

  // Cleanup on unmount (project switch) — empty deps, only runs on true unmount
  useEffect(() => {
    return () => {
      if (_recorder && _recorder.state !== 'inactive') {
        _recorder.stop();
        _recorder.stream.getTracks().forEach(t => t.stop());
      }
      _recorder = null;
      useAppStore.getState().setRecording(null);
    };
  // deps intentionally empty — cleanup on unmount only
  }, []);
}

/**
 * Orchestrates the Cmd+D voice input flow: recording → sync upload → text insertion.
 *
 * Listens for recording completion when a voice widget is active in the editor.
 * Uploads audio to the sync endpoint, inserts transcribed text at widget position,
 * and adds the reference to the store.
 *
 * Two recording flows:
 *
 * A) Cmd+D inline widget (voice widget active):
 *    recorder.onstop → handleVoiceRecording finds widget → sync upload to
 *    /references/upload-and-transcribe → on success: remove widget, insert text
 *    at cursor position, update reference → on error: remove widget, emit error
 *
 * B) Header REC button (no voice widget):
 *    recorder.onstop → handleVoiceRecording returns false → async fallback
 *    creates temp reference → uploads to /references/upload → backend handles
 *    async transcription → WS event updates reference status to "ready"
 */
// SYSTEM: voice-input — Cmd+D voice recording → transcription → text insertion pipeline

import { useCallback } from 'react';
import { useAppStore } from '../store/app-store';
import { emit } from '../events';
import { voiceWidgetField, voiceWidgetRemove } from '../components/editor/voice-widget';
import { getEditorView } from '../editor/active-editor';
import { isVoiceCancelled } from '../components/editor/voice-cancel-flag';
import { deleteActiveSession } from '../utils/recording-cache';

/**
 * Call from ProjectPage alongside useAudioRecorder.
 *
 * Returns a handleVoiceRecording function that should be called before
 * the regular handleRecordingComplete. If it returns true, the recording
 * was handled by the voice widget flow and the regular flow should be skipped.
 */
export function useVoiceInput() {
  const updateReference = useAppStore(s => s.updateReference);

  const handleVoiceRecording = useCallback(async (
    file: File,
    ctx: { projectId: string; documentId: string; sessionId: string },
  ): Promise<boolean> => {
    // Escape was pressed — discard recording entirely
    if (isVoiceCancelled()) return true;

    const view = getEditorView();
    if (!view) return false;

    let widgetState;
    try {
      widgetState = view.state.field(voiceWidgetField);
    } catch {
      return false;
    }
    if (!widgetState) return false;

    // Voice widget is active — handle via sync endpoint
    const formData = new FormData();
    formData.append('file', file);
    formData.append('project_id', ctx.projectId);
    if (ctx.documentId) formData.append('document_id', ctx.documentId);
    // Replay guard: THIS take's cache session id (carried by ctx since record
    // start — never a global read) makes a re-send replay the SAME reference
    // instead of duplicating it (SYSTEM: recording-cache).
    formData.append('idempotency_key', ctx.sessionId);

    try {
      // WHY raw fetch instead of apiClient.upload: upload-and-transcribe
      // transcribes synchronously, and fetchWithRetry would blindly re-POST on
      // 502/503 — safe for duplicates since the idempotency key above, but the
      // widget's UX contract is fail-fast: no retry hangs, immediate error
      // surface, the take stays in the cache for the boot restore.
      const res = await fetch('/api/references/upload-and-transcribe', {
        method: 'POST',
        credentials: 'include',
        body: formData,
      });

      if (!res.ok) {
        const errorData = await res.json().catch(() => ({}));
        console.error('Voice transcription failed:', errorData);
        // Transcription failed — remove widget immediately, no retries. The backend may
        // have created a reference (side-effect of save_upload); user feedback for the
        // failure is surfaced by the voice-transcription-error listener
        // (see useVoiceTranscriptionError) → toast.
        view.dispatch({ effects: voiceWidgetRemove.of(undefined) });
        emit('voice-transcription-error', { editorView: view });
        return true;
      }

      const data = await res.json();
      // 2xx ack → the take is safely on the server; drop the cached copy
      // (INVARIANT in recording-cache: a record dies only after a 2xx or a
      // user discard — on any failure it stays for the boot restore).
      void deleteActiveSession();
      const { text, reference_id: refId } = data;

      // Check if widget was cancelled (Escape) while fetch was in flight
      let currentWidgetState;
      try {
        currentWidgetState = view.state.field(voiceWidgetField);
      } catch { /* field may not exist */ }

      if (!currentWidgetState) {
        // Widget was cancelled — still update reference status
        updateReference(refId, {
          processing_status: 'ready',
          content: text,
          updated_at: new Date().toISOString(),
        });
        return true;
      }

      const insertPos = currentWidgetState.pos;

      // Remove widget first, then insert text
      view.dispatch({ effects: voiceWidgetRemove.of(undefined) });
      view.dispatch({
        changes: { from: insertPos, insert: text },
        selection: { anchor: insertPos + text.length },
      });

      // Update reference status — preserve file_path/file_meta from WS-fetched ref
      updateReference(refId, {
        processing_status: 'ready',
        content: text,
        updated_at: new Date().toISOString(),
      });

      emit('voice-transcription-complete', { text, referenceId: refId, editorView: view });
      return true;
    } catch (err) {
      console.error('Voice input error:', err);
      view.dispatch({ effects: voiceWidgetRemove.of(undefined) });
      emit('voice-transcription-error', { editorView: view });
      return true;
    }
  }, [updateReference]);

  return { handleVoiceRecording };
}

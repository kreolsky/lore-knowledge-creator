/** Cmd+D global hotkey for toggling voice recording when editor is not focused. */
// ARCH: When editor IS focused, CM6 keymap handles Cmd+D (inline voice widget).
// Exception: if a REC-button recording is active, CM6 returns false and event bubbles here.

import { useEffect } from 'react';
import { useAppStore } from '../store/app-store';
import { emit } from '../events';
import { getEditorView } from '../editor/active-editor';
import { t as tImperative } from '../i18n';

export function useRecordingHotkey() {
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.key.toLowerCase() !== 'd') return;

      const { recording, currentDocument, accessLevel } = useAppStore.getState();
      const view = getEditorView();

      if (view?.hasFocus && !recording) return;

      e.preventDefault();

      if (!currentDocument && !recording) return;

      if (recording) {
        emit('stop-recording');
      } else if (accessLevel !== 'full') {
        useAppStore.getState().showToast(tImperative('recordingRequiresAccess'));
      } else {
        emit('start-recording');
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, []);
}

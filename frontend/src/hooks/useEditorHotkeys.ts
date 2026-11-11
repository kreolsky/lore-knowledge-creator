/** Editor keyboard shortcuts. Currently: Cmd/Ctrl+S → snapshot modal (document column only). */

import { useEffect } from 'react';
import { useAppStore } from '../store/app-store';
import { useEditorContent, getFocusedRole, type EditorRole, getActiveTablesJson } from '../editor/active-editor';

interface UseEditorHotkeysOpts {
  role: EditorRole;
  isReference: boolean;
}

/**
 * Binds editor-global keyboard shortcuts to `window`.
 *
 * INVARIANT: snapshot (Cmd+S) targets the document only. The secondary (reference)
 * instance never binds; the primary additionally checks getFocusedRole() so that
 * hitting Cmd+S while the reference column is focused is a no-op rather than  Why: Cmd+S targets the document; the secondary (reference) instance never binds, and the primary checks getFocusedRole() so Cmd+S with the reference focused is a no-op.
 * snapshotting the ref. Why: split view mounts two editors but only the document
 * column owns snapshots.
 */
export function useEditorHotkeys({ role, isReference }: UseEditorHotkeysOpts): void {
  const getContent = useEditorContent();
  useEffect(() => {
    if (role === 'secondary') return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.shiftKey && e.key.toLowerCase() === 's') {
        e.preventDefault();
        if (!isReference && getFocusedRole() !== 'secondary' && useAppStore.getState().accessLevel === 'full'
          // INVARIANT(corruption): no manual snapshot while a snapshot preview is open.
          // Why: the preview owns the root slot, so getContent() would capture the
          // CHECKPOINT's historical text and store it as a fresh "current" snapshot.
          // Exit the preview first; HistoryPanel's saveSnapshot button remains the
          // live-text snapshot path.
          && !useAppStore.getState().snapshotPreview) {
          useAppStore.getState().openSnapshotModal(getContent(), getActiveTablesJson());
        }
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isReference, role, getContent]);
}

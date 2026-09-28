/**
 * CM6 extension: drag-and-drop a text/.docx file onto the editor body to import
 * its content (cleaned by the backend) below the drop position.
 *
 * ARCH: Uses EditorView.domEventHandlers to intercept dragover + drop before
 * the browser default (which would navigate to / open the file). The extension
 * is a FACTORY taking an `onDrop(file, pos)` callback because opening a React
 * confirmation modal can't happen inside a plain CM6 extension — the callback
 * is supplied by Editor.tsx, which owns the modal state.
 * SYSTEM: editor — file-drop import handler
 */

import type { Extension } from '@codemirror/state';
import { EditorView } from '@codemirror/view';

function hasFiles(e: DragEvent): boolean {
  return !!e.dataTransfer?.types?.includes('Files');
}

/**
 * Build a drop-handler extension. `onDrop` receives the first file and the
 * document position at the drop coordinates (falling back to the selection head
 * when the coordinates don't map to a position).
 */
export function makeFileDropExtension(onDrop: (file: File, pos: number) => void): Extension {
  return EditorView.domEventHandlers({
    dragover(e) {
      if (hasFiles(e)) {
        e.preventDefault();
        return true;
      }
      return false;
    },
    drop(e, view) {
      if (!hasFiles(e)) return false;
      e.preventDefault();
      e.stopPropagation();
      const file = e.dataTransfer?.files?.[0];
      if (!file) return true;
      const pos = view.posAtCoords({ x: e.clientX, y: e.clientY });
      onDrop(file, pos ?? view.state.selection.main.head);
      return true;
    },
  });
}

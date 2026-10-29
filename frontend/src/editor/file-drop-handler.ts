/**
 * CM6 extension: drag-and-drop an image/text/.docx file onto the editor body to
 * import it at the drop position (images upload as references and embed;
 * text/.docx content is cleaned by the backend).
 *
 * ARCH: CAPTURE-phase dragover/drop listeners on the document view's root, not
 * EditorView.domEventHandlers. Why: a mounted table cell is a nested EditorView —
 * the innermost drop target — and CM6's built-in drop handler would insert the
 * file as TEXT into the cell. Capture on the root fires first; stopPropagation
 * closes the nested path for files.
 *
 * A FACTORY taking `onDrop(file, {hostPos, x, y})`: opening a React confirmation
 * modal can't happen inside a plain CM6 extension (useEditorFileDrop supplies it).
 * hostPos is the host document position (selection-head fallback); routing into
 * a table CELL is resolved later from x/y (drop-target.editorDropPoint).
 * SYSTEM: editor — file-drop import handler
 */

import type { Extension } from '@codemirror/state';
import { ViewPlugin } from '@codemirror/view';

function hasFiles(e: DragEvent): boolean {
  return !!e.dataTransfer?.types?.includes('Files');
}

export interface FileDropCoords {
  hostPos: number;
  x: number;
  y: number;
}

export function makeFileDropExtension(onDrop: (file: File, coords: FileDropCoords) => void): Extension {
  return ViewPlugin.define((view) => {
    const onDragOver = (e: DragEvent) => { if (hasFiles(e)) e.preventDefault(); };
    const handleDrop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.stopPropagation();
      const file = e.dataTransfer?.files?.[0];
      if (!file) return;
      const pos = view.posAtCoords({ x: e.clientX, y: e.clientY });
      onDrop(file, { hostPos: pos ?? view.state.selection.main.head, x: e.clientX, y: e.clientY });
    };
    view.dom.addEventListener('dragover', onDragOver, true);
    view.dom.addEventListener('drop', handleDrop, true);
    return {
      destroy() {
        view.dom.removeEventListener('dragover', onDragOver, true);
        view.dom.removeEventListener('drop', handleDrop, true);
      },
    };
  });
}

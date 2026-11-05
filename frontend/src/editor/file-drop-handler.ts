/**
 * CM6 extension: drag-and-drop an image/text/.docx file onto the editor body to
 * import it at the drop position (images upload as references and embed;
 * text/.docx content is cleaned by the backend). A clipboard image pasted into the
 * editor takes the same image path, at the caret.
 *
 * ARCH: CAPTURE-phase dragover/drop listeners on the document view's root, not
 * EditorView.domEventHandlers. Why: a mounted table cell is a nested EditorView —
 * the innermost drop target — and CM6's built-in drop handler would insert the
 * file as TEXT into the cell. Capture on the root fires first; stopPropagation
 * closes the nested path for files. Paste is captured on the root for the same reason.
 *
 * A FACTORY taking `onDrop(files, {hostPos, x, y})`: opening a React confirmation
 * modal can't happen inside a plain CM6 extension (useEditorFileDrop supplies it).
 * hostPos is the host document position (selection-head fallback); routing into
 * a table CELL is resolved later from x/y (drop-target.editorDropPoint).
 * SYSTEM: editor — file-drop import handler
 */

import type { Extension } from '@codemirror/state';
import { EditorView, ViewPlugin } from '@codemirror/view';

function hasFiles(e: DragEvent): boolean {
  return !!e.dataTransfer?.types?.includes('Files');
}

export interface FileDropCoords {
  hostPos: number;
  x: number;
  y: number;
}

/**
 * The images a paste should upload, or [] when it is a text paste.
 * INVARIANT(data-loss): any clipboard text wins over an image flavor.
 * Why: Word/Excel add a raster of the selection next to its text, and a Lore image copy
 * adds image/png next to its `ref:` markdown — uploading would replace editable content
 * with a picture and duplicate the reference. Plain text equal to the file's name is a
 * file manager's copy, not content.
 */
export function clipboardImages(data: DataTransfer): File[] {
  // WHY `?? []`: synthetic paste events (jsdom tests, embedded webviews) can carry
  // a text-only clipboardData with no files list — that is "no images", not a crash.
  const images = Array.from(data.files ?? []).filter((f) => f.type.startsWith('image/'));
  const plain = data.getData('text/plain').trim();
  if (plain && !images.some((f) => f.name === plain)) return [];
  return images;
}

export function makeFileDropExtension(
  onDrop: (files: File[], coords: FileDropCoords) => void,
  onPasteImages: (images: File[], view: EditorView, pos: number) => void,
): Extension {
  return ViewPlugin.define((view) => {
    const onDragOver = (e: DragEvent) => { if (hasFiles(e)) e.preventDefault(); };
    const handleDrop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.stopPropagation();
      const files = Array.from(e.dataTransfer?.files ?? []);
      if (files.length === 0) return;
      const pos = view.posAtCoords({ x: e.clientX, y: e.clientY });
      onDrop(files, { hostPos: pos ?? view.state.selection.main.head, x: e.clientX, y: e.clientY });
    };
    const handlePaste = (e: ClipboardEvent) => {
      const images = e.clipboardData ? clipboardImages(e.clipboardData) : [];
      if (images.length === 0) return;
      e.preventDefault();
      e.stopPropagation();
      // WHY the event target: focus may sit in a table cell, a nested view of this root.
      const cm = e.target instanceof Element ? e.target.closest('.cm-editor') : null;
      const target = (cm && EditorView.findFromDOM(cm as HTMLElement)) ?? view;
      onPasteImages(images, target, target.state.selection.main.to);
    };
    view.dom.addEventListener('dragover', onDragOver, true);
    view.dom.addEventListener('drop', handleDrop, true);
    view.dom.addEventListener('paste', handlePaste, true);
    return {
      destroy() {
        view.dom.removeEventListener('dragover', onDragOver, true);
        view.dom.removeEventListener('drop', handleDrop, true);
        view.dom.removeEventListener('paste', handlePaste, true);
      },
    };
  });
}

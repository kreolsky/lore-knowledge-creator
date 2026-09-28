/**
 * Scroll-to-line listener: TOC click ('scroll-to-line' event) → CM6
 * scrollIntoView for that heading line.
 *
 * ARCH: shared by the authed editor (via useEditorEvents) and the public
 * /s/:token viewer (PublicEditor) — the listener used to live inline in
 * useEditorEvents ONLY, so TOC clicks on the public surface fired into an
 * empty bus and did nothing. enabled=false silences the secondary split
 * column (same contract as useEditorEvents' other listeners).
 */

import { useCallback } from 'react';
import { EditorView } from '@codemirror/view';
import { useEvent } from './useEvent';

interface UseScrollToLineParams {
  editorViewRef: React.RefObject<EditorView | null>;
  /** WHY: false on the split-view secondary column — one listener must own the scroll. */
  enabled?: boolean;
}

export function useScrollToLine({ editorViewRef, enabled = true }: UseScrollToLineParams) {
  useEvent('scroll-to-line', useCallback(({ line }: { line: number }) => {
    if (!enabled) return;
    if (!editorViewRef.current) return;
    const view = editorViewRef.current;
    const doc = view.state.doc;
    if (line >= 1 && line <= doc.lines) {
      const lineInfo = doc.line(line);
      view.dispatch({
        effects: EditorView.scrollIntoView(lineInfo.from, { y: 'start', yMargin: 60 }),
      });
    }
  }, [editorViewRef, enabled]));
}

import type React from 'react';

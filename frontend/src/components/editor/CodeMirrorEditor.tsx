/**
 * Thin React wrapper around raw CodeMirror 6 EditorView.
 *
 * WHY not @uiw/react-codemirror: @uiw injects styles via StyleModule with
 * high specificity (requiring !important overrides), rebuilds extensions on
 * every render (requiring React.memo(() => true) hack), and adds an opaque
 * abstraction layer over CM6 lifecycle. Raw EditorView gives full control.
 *
 * Remount strategy: use key={id} on the parent to unmount/remount on document switch.
 */

import type React from 'react';
import { useRef, useEffect } from 'react';
import { EditorView, keymap, dropCursor, rectangularSelection, crosshairCursor, highlightSpecialChars } from '@codemirror/view';
import { EditorState, StateEffect, type Extension } from '@codemirror/state';
import { history, defaultKeymap, historyKeymap } from '@codemirror/commands';
import { search as searchExt, highlightSelectionMatches } from '@codemirror/search';
import { closeBrackets, closeBracketsKeymap, autocompletion, completionKeymap } from '@codemirror/autocomplete';
import { bracketMatching, indentOnInput, syntaxHighlighting, defaultHighlightStyle, syntaxTreeAvailable } from '@codemirror/language';
import { autoSurround } from './auto-surround';
import { autoFence } from './auto-fence';
import { autoFenceSelection } from './auto-fence-selection';
import { smartQuotes } from './smart-quotes';
import { thickCursorLayer } from './thick-cursor';
import { searchHighlight, searchPanelListenerCompartment } from './find-matches';

/**
 * Standard CM6 extensions replacing @uiw's basicSetup (without lineNumbers,
 * foldGutter, highlightActiveLine), parameterized by context.
 *
 * `search`    — include the search() StateField (Find panel query). The stock
 *   bottom panel is never opened; Mod-f is rebound to openFind. Off for small
 *   inline editors.
 * `thickCursor` — include the thick caret layer. On for the document/cell,
 *   off for mini inline editors.
 */
export function cmSetup({ search = true, thickCursor = true }: { search?: boolean; thickCursor?: boolean } = {}): Extension {
  return [
    highlightSpecialChars(),
    history(),
    ...(thickCursor ? [thickCursorLayer] : []),
    dropCursor(),
    EditorState.allowMultipleSelections.of(true),
    indentOnInput(),
    syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
    bracketMatching(),
    autoFenceSelection,
    autoSurround,
    autoFence,
    smartQuotes,
    closeBrackets(),
    autocompletion(),
    rectangularSelection(),
    crosshairCursor(),
    highlightSelectionMatches(),
    // WHY: search() provides the search query StateField so FindReplacePanel can
    // dispatch setSearchQuery/findNext programmatically. The stock bottom panel is
    // never opened — Mod-f is rebound to openFind (Prec.high buildKeymapExtension),
    // and searchKeymap (which bound Mod-f → openSearchPanel) is intentionally dropped.
    // searchHighlight decorates EVERY query match (the stock searchHighlighter refuses
    // to render without its own panel — see find-matches.ts). The listener compartment
    // is kept inert here; FindReplacePanel reconfigures it on mount for the live count.
    ...(search ? [searchExt(), searchHighlight, searchPanelListenerCompartment.of([])] : []),
    keymap.of([
      ...closeBracketsKeymap,
      ...defaultKeymap,
      ...historyKeymap,
      ...completionKeymap,
    ]),
  ];
}

interface CodeMirrorEditorProps {
  doc: string;
  extensions?: Extension[];
  /** Initial cursor position — set at EditorState creation so decorations build with correct cursor context. */
  initialCursor?: number;
  /** Called once parser has settled and decorations are applied — safe to restore scroll and show editor. */
  onReady?: () => void;
  onCreateView?: (view: EditorView) => void;
  className?: string;
  style?: React.CSSProperties;
  onClick?: React.MouseEventHandler<HTMLDivElement>;
}

// ARCH: onReady fires after syntax tree covers the viewport + double rAF,
// ensuring visible decorations are built before scroll restore. Viewport-only
// check allows fast initial paint for large documents. scrollAnchorPlugin
// handles any height-model shifts from incremental parsing beyond the viewport.
export default function CodeMirrorEditor({ doc, extensions = [], initialCursor, onReady, onCreateView, className, style, onClick }: CodeMirrorEditorProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const selection = initialCursor != null
      ? { anchor: Math.min(initialCursor, doc.length) }
      : undefined;
    const state = EditorState.create({ doc, extensions, selection });
    const view = new EditorView({ state, parent: containerRef.current });
    onCreateView?.(view);

    // Signal readiness after syntax tree covers viewport + two animation frames.
    // WHY viewport check: CM6's incremental parser emits partial trees. First tree
    // change may cover only a few hundred chars — decorations for headings/code blocks
    // aren't computed yet. syntaxTreeAvailable(state, viewport.to) ensures the parser
    // has covered all visible content, so StateField decorations are complete.
    // WHY double rAF: 1st frame flushes CM6 DOM update (decorations → DOM),
    // 2nd frame ensures the browser has painted them (line heights are final).
    let settled = false;
    const listener = EditorView.updateListener.of((update) => {
      if (settled) return;
      if (syntaxTreeAvailable(update.state, update.view.viewport.to)) {
        settled = true;
        requestAnimationFrame(() => requestAnimationFrame(() => onReady?.()));
      }
    });
    view.dispatch({ effects: StateEffect.appendConfig.of(listener) });
    // Fallback: empty/short docs where parser finishes synchronously before listener
    requestAnimationFrame(() => {
      if (!settled && syntaxTreeAvailable(view.state, view.viewport.to)) {
        settled = true;
        requestAnimationFrame(() => onReady?.());
      }
    });

    return () => { view.destroy(); };
  // deps intentionally empty — create editor once on mount
  }, []);

  return <div ref={containerRef} className={className} style={style} onClick={onClick} />;
}

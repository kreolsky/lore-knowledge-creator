/**
 * Shared, render-only CodeMirror extension bundle + the unified editor-experience factory.
 *
 * SYSTEM: editor — single source of truth for the editor "experience" (visual rendering
 * AND the editing stack) so the main document editor (Editor.tsx), table cells, and
 * nested transclusion views render and behave identically by construction. Add a feature
 * once here and it appears in every context.
 *
 * ARCH: two layers, one source each.
 *  - `liveFields({ tableBlock })` — the *visual* group (live-preview replace/widget
 *    decorations + syntax coloring + ligatures). `renderExtensions()` is the document
 *    alias (`tableBlock: true`) consumed by Editor.tsx's renderCompartment (Mod-/
 *    plain-text toggle). Cells/nested pass `tableBlock: false` (no recursive table
 *    objects inside an embedded view).
 *  - `editorExperience({ mode, depth, editable })` — the shared core around the visual
 *    group: indentUnit, markdown, line-wrapping, transclusion depth, reveal-at-cursor,
 *    theme, plus the editing stack (setup + formatting hotkeys + thick caret) when
 *    editable or the read-only bits when not. `editableCellExtensions()` and
 *    `nestedRenderExtensions(depth)` are thin aliases. The document routes through it too
 *    (Editor.tsx, mode `document`) but keeps its render fields in the Mod-/ compartment.
 */
import { type Extension } from '@codemirror/state';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { indentUnit } from '@codemirror/language';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { languages } from '@codemirror/language-data';
import { markdownExtensions } from '../../editor/markdown-config';
import { headingHighlight, bareUrlPlugin } from '../../editor/editor-plugins';
import { copyHandlerExtension } from '../../editor/copy-handler';
import { codeHighlightStyle } from '../../editor/code-highlight';
import { ligaturePlugin } from './ligature-plugin';
import { thickCursorLayer } from './thick-cursor';
import { cmSetup } from './CodeMirrorEditor';
import { buildKeymapExtension } from './hotkey-keymap';
import { DEFAULT_HOTKEYS } from './hotkey-config';
import { loreEditorTheme } from './theme';
import {
  livePreviewField,
  listLinePlugin,
  cursorLinePlugin,
  tableRenderField,
  tableBlockField,
  mathBlockRenderField,
  mermaidBlockRenderField,
  transclusionDepth,
  revealAtCursor,
} from './live-preview';

/**
 * Everything visual: structural rendering (replace/widgets) + syntax coloring
 * (heading sizes, bold/italic, code/link colors) + typographic ligatures.
 * Toggled off as a group for the plain-text "notepad" mode (Mod-/) in Editor.tsx.
 *
 * `tableBlock` includes the editable table block widget — true for the document,
 * false for cells/nested views (a table object never recurses inside another).
 *
 * WHY a function, not a const array: this module sits in an import cycle with
 * live-preview (render-bundle → live-preview → fields → build-structural → widgets →
 * render-bundle). A module-eval array literal would capture still-undefined live-preview
 * bindings; reading them at call time (after all modules evaluate) yields the real values.
 */
export function liveFields({ tableBlock }: { tableBlock: boolean }): Extension {
  return [
    livePreviewField,
    listLinePlugin,
    cursorLinePlugin,
    tableRenderField,
    ...(tableBlock ? [tableBlockField] : []),
    mathBlockRenderField,
    mermaidBlockRenderField,
    headingHighlight,
    codeHighlightStyle,
    bareUrlPlugin,
    ligaturePlugin,
  ];
}

/** Document-editor alias: the full visual group (with table block objects), toggled by Mod-/. */
export function renderExtensions(): Extension {
  return liveFields({ tableBlock: true });
}

/**
 * Theme overrides for a nested (transclusion) view: the band supplies the gray and
 * the centered column, so the inner scroller drops its own padding/background/min-height
 * and lets content drive height. `.cm-content` keeps the host column width so embedded
 * text aligns with the host document.
 */
const nestedThemeOverride = EditorView.theme({
  '&': { background: 'transparent' },
  '.cm-scroller': {
    padding: '0 !important',
    background: 'transparent !important',
    overflow: 'visible !important',
  },
  // Keep `.cm-content` at the host column width (844px, centered — set by index.css)
  // so embedded text aligns with the host document; only drop the min-height.
  '.cm-content': {
    padding: '0 !important',
    minHeight: '0 !important',
  },
});

/**
 * Theme for an in-cell editor. A table cell is NOT a mini-document: it has its own
 * compact typography that OVERRIDES the document's. Crucially this neutralizes
 * `loreEditorTheme`'s `.cm-scroller { padding: 2rem }` and its document-scale heading
 * sizes / heading top-spacing — left in place they make every cell huge.
 *
 * INVARIANT: a cell strips the document's scroller padding, font size, line-height and
 * heading scale. Why: `editorExperience({mode:'cell'})` shares `loreEditorTheme` (one
 * source of truth); this override is the table's own skin layered last so cells stay
 * compact and a click anywhere in the cell lands in its editor.
 */
const cellThemeOverride = EditorView.theme({
  // INVARIANT: the cell editor is content-sized, never a forced height:100%. Why: a cell
  // cannot fill a row made tall by a multi-line SIBLING — percentage height does not resolve
  // through a table cell whose row height is content-determined, so height:100% here is a
  // no-op. Content-height keeps CM6's viewport == all of THIS cell's lines (every line
  // clickable); the dead space below a short cell in a tall row is covered by the cell's
  // mousedown router (table-block-widget), which routes such a click into the editor via
  // posAtCoords.
  '&': { background: 'transparent' },
  '.cm-scroller': {
    padding: '0 !important', // kill loreEditorTheme's 2rem document margin inside a cell
    background: 'transparent !important',
    overflow: 'visible !important',
    fontFamily: 'inherit',
    lineHeight: '1.4',
  },
  '.cm-content': {
    padding: '4px 8px !important',
    fontSize: '0.875em',
    lineHeight: '1.4',
    caretColor: 'transparent', // cell uses the thick-layer caret (loreEditorTheme parity)
  },
  '.cm-line': { padding: '0 !important' },
  // Cells are compact — no document-scale heading sizes and no heading top-spacing.
  '.cm-header-1, .cm-header-2, .cm-header-3, .cm-header-4, .cm-header-5, .cm-header-6': {
    fontSize: '1em !important',
    lineHeight: 'inherit !important',
  },
  '.cm-line:has(.cm-header-1), .cm-line:has(.cm-header-2), .cm-line:has(.cm-header-3), .cm-line:has(.cm-header-4), .cm-line:has(.cm-header-5), .cm-line:has(.cm-header-6)': {
    paddingTop: '0 !important',
  },
});

// Cell hotkeys = the document's hotkeys MINUS the ones that make no sense (or are unsafe)
// inside a cell: insertTable (would nest a table object), voiceInput (targets the doc).
const CELL_HOTKEYS = Object.fromEntries(
  Object.entries(DEFAULT_HOTKEYS).filter(([, a]) => a !== 'insertTable' && a !== 'voiceInput' && a !== 'workWithSelection'),
);

/** The markdown language config — identical in every editor context. */
function markdownLang(): Extension {
  return markdown({ base: markdownLanguage, codeLanguages: languages, addKeymap: false, extensions: markdownExtensions });
}

/**
 * THE single editor-experience factory. One source of truth for what it means to edit
 * (or render) markdown in this app, parameterized by context:
 *  - `document` — the main editor. Gets the search-enabled `cmSetup` (Find panel).
 *    Its visual render group lives in Editor.tsx's Mod-/ compartment, so this factory
 *    intentionally OMITS `liveFields` for the document; everything else is shared.
 *  - `cell` — a table cell's nested EditorView. Search-free `cmSetup`, the
 *    document's formatting hotkeys (minus cell-unsafe ones), thick caret, table-less
 *    visual group, cell chrome. Bound to the cell's Y.Text by the caller.
 *  - `nested` — a read-only transclusion view at `depth`. No editing stack.
 *
 * INVARIANT: reveal-at-cursor is on for any editable context and for a depth-0 read-only  Why: reveal is on for editable + depth-0 read-only; a depth>0 nested view never reveals (its position-0 selection would show the first element raw).
 * view; a depth>0 nested view never reveals raw markdown (its selection defaults to pos 0,
 * which would otherwise show the first element raw). This is the ONE place the
 * depth→behaviour mapping lives. Why: every decoration builder reads the single
 * `revealAtCursor` facet, so future builders inherit the rule for free.
 */
export function editorExperience(opts: {
  mode: 'document' | 'cell' | 'nested';
  depth?: number;
  editable?: boolean;
}): Extension {
  const { mode } = opts;
  const depth = opts.depth ?? (mode === 'cell' ? 1 : 0);
  const editable = opts.editable ?? mode !== 'nested';

  const ext: Extension[] = [
    indentUnit.of('\t'),
    markdownLang(),
    EditorView.lineWrapping,
    transclusionDepth.of(depth), // cap nested transclusion (no infinite embedding)
    revealAtCursor.of(editable ? true : depth === 0),
    loreEditorTheme,
    // Rich copy (text/html + portable markdown) for EVERY context: the document, table
    // cells, and read-only transclusion views all format on copy. Paste stays
    // document-only (see Editor.tsx) — importing a table into a cell is invalid.
    copyHandlerExtension,
  ];

  if (mode === 'document') {
    // cmSetup already includes the thick caret + search. Render fields + Mod-/
    // compartment stay entity-specific in Editor.tsx.
    ext.unshift(cmSetup({ search: true, thickCursor: true }));
    return ext;
  }

  if (mode === 'cell') {
    ext.push(
      // Same inline experience as the document: smart quotes, auto-surround/pairs,
      // history, bracket matching, autocompletion + the base keymap (no search).
      cmSetup({ search: false, thickCursor: false }),
      // The document's formatting hotkeys (bold/italic/quote/lists/…), minus cell-unsafe ones.
      buildKeymapExtension(CELL_HOTKEYS),
      liveFields({ tableBlock: false }),
      thickCursorLayer,
      cellThemeOverride,
    );
    return ext;
  }

  // nested (read-only)
  ext.push(
    EditorView.editable.of(false),
    EditorState.readOnly.of(true),
    liveFields({ tableBlock: false }),
    nestedThemeOverride,
  );
  return ext;
}

/**
 * EDITABLE extension set for a single table cell — markdown rendering identical to the
 * document plus the thick caret, but no recursive table objects. One nested EditorView
 * per cell, bound to the cell's Y.Text via `createYjsExtension`.
 */
export function editableCellExtensions(): Extension {
  return editorExperience({ mode: 'cell', depth: 1 });
}

/**
 * Full read-only extension set for a standalone nested transclusion view, seeded at
 * `depth` (the host is depth 0; a nested view is depth+1 — caps recursion).
 */
export function nestedRenderExtensions(depth: number): Extension {
  return editorExperience({ mode: 'nested', depth, editable: false });
}

/**
 * Markdown formatting actions for CodeMirror 6.
 *
 * Each action takes an EditorView and returns true if handled.
 * The action registry is a flat lookup dictionary keyed by MarkdownAction.
 */
// ARCH: Action registry pattern — actions decoupled from keybindings, reused by SelectionToolbar and hotkeys.

import { EditorView } from '@codemirror/view';
import { syntaxTree } from '@codemirror/language';
import type { SyntaxNode } from '@lezer/common';
import type { MarkdownAction } from './hotkey-config';
import { emit } from '../../events';
import { useAppStore } from '../../store/app-store';
import { useNoteChatStore } from '../../store/note-chat-store';
import { useNoteStore } from '../../store/note-store';
import { useUIStore } from '../../store/ui-store';
import { getActiveHandle, getFocusedIsReference } from '../../editor/active-editor';
import { refIsScope } from '../../store/ui-store/documents-slice';
import * as Y from 'yjs';
import { voiceWidgetField, voiceWidgetStart, voiceWidgetRecognizing } from './voice-widget';
import { preserveCursorFromLineEnd, snapSelectionToLineEnd } from './cursor-preserve';
import { createTable, tableAnchor } from './live-preview/table-block-model';
import { requestTableFocus } from './live-preview/table-block-widget';
import { createMarkdownReference } from '../references/create-markdown-reference';
import { agentAction } from './markdown-actions-agent';
import { t } from '../../i18n';

// WHY: marker → Lezer node name map. Used to detect "cursor is inside formatted span"
// so empty-selection hotkeys can unwrap instead of inserting `****` litter.
const MARKER_NODE: Record<string, string> = {
  '**': 'StrongEmphasis',
  '*': 'Emphasis',
  '~~': 'Strikethrough',
  '`': 'InlineCode',
};

/** Walk up the syntax tree from a position; return the nearest ancestor matching `nodeName`. */
function findAncestorOfType(state: EditorView['state'], pos: number, nodeName: string): SyntaxNode | null {
  const tree = syntaxTree(state);
  // Check both sides so a caret sitting exactly on a span boundary still resolves inside.
  for (const side of [-1, 1] as const) {
    for (let cur: SyntaxNode | null = tree.resolveInner(pos, side); cur; cur = cur.parent) {
      if (cur.name === nodeName) return cur;
    }
  }
  return null;
}

/** Toggle symmetric inline markers (e.g. ** for bold, ~~ for strikethrough). */
function toggleInlineMarker(view: EditorView, marker: string): boolean {
  const { from, to, empty } = view.state.selection.main;
  const markerLen = marker.length;

  const nodeName = MARKER_NODE[marker];

  // Unwrap a formatted ancestor span when the caret/selection lies fully inside it.
  // Selection inside the same kind of formatting → strip surrounding markers, keep selection.
  // Empty caret inside the span → strip markers, keep caret.
  if (nodeName) {
    const node = findAncestorOfType(view.state, from, nodeName);
    if (node && node.from <= from && node.to >= to) {
      const innerStart = node.from + markerLen;
      const innerEnd = node.to - markerLen;
      const clampedFrom = Math.max(innerStart, Math.min(innerEnd, from));
      const clampedTo = Math.max(innerStart, Math.min(innerEnd, to));
      const newFrom = node.from + (clampedFrom - innerStart);
      const newTo = node.from + (clampedTo - innerStart);
      view.dispatch({
        changes: [
          { from: node.from, to: node.from + markerLen, insert: '' },
          { from: node.to - markerLen, to: node.to, insert: '' },
        ],
        selection: empty ? { anchor: newFrom } : { anchor: newFrom, head: newTo },
      });
      return true;
    }
  }

  if (empty) {
    // No matching formatted ancestor. If the caret is inside a word, wrap that word;
    // otherwise consume the hotkey silently (avoid stray `****` litter at empty caret).
    const word = view.state.wordAt(from);
    if (word && word.from < word.to) {
      const text = view.state.sliceDoc(word.from, word.to);
      view.dispatch({
        changes: { from: word.from, to: word.to, insert: marker + text + marker },
        selection: { anchor: from + markerLen },
      });
    }
    return true;
  }

  const selected = view.state.sliceDoc(from, to);

  // Check if already wrapped — unwrap
  if (selected.startsWith(marker) && selected.endsWith(marker) && selected.length >= markerLen * 2) {
    const inner = selected.slice(markerLen, -markerLen);
    view.dispatch({
      changes: { from, to, insert: inner },
      selection: { anchor: from, head: from + inner.length },
    });
    return true;
  }

  // Check surrounding context — unwrap outer markers
  const before = view.state.sliceDoc(Math.max(0, from - markerLen), from);
  const after = view.state.sliceDoc(to, Math.min(view.state.doc.length, to + markerLen));
  if (before === marker && after === marker) {
    view.dispatch({
      changes: [
        { from: from - markerLen, to: from, insert: '' },
        { from: to, to: to + markerLen, insert: '' },
      ],
      selection: { anchor: from - markerLen, head: to - markerLen },
    });
    return true;
  }

  // Wrap selection
  view.dispatch({
    changes: { from, to, insert: marker + selected + marker },
    selection: { anchor: from, head: from + markerLen + selected.length + markerLen },
  });
  return true;
}

/** Set or toggle heading level on the current line. */
function setHeading(view: EditorView, level: number): boolean {
  const { from } = view.state.selection.main;
  const line = view.state.doc.lineAt(from);
  const prefix = '#'.repeat(level) + ' ';

  // Strip existing heading prefix
  const match = line.text.match(/^(#{1,6})\s/);
  if (match) {
    const existingPrefix = match[0];
    // Same level — toggle off (remove heading)
    if (match[1].length === level) {
      view.dispatch({
        changes: { from: line.from, to: line.from + existingPrefix.length, insert: '' },
      });
      return true;
    }
    // Different level — replace
    view.dispatch({
      changes: { from: line.from, to: line.from + existingPrefix.length, insert: prefix },
    });
    return true;
  }

  // No heading — add prefix
  view.dispatch({ changes: { from: line.from, insert: prefix } });
  return true;
}

/** Prepend a tab to each line in the selection. */
function indentLines(view: EditorView): boolean {
  const sel = view.state.selection.main;
  const startLine = view.state.doc.lineAt(sel.from);
  const endLine = view.state.doc.lineAt(sel.to);
  const changes: { from: number; insert: string }[] = [];

  for (let i = startLine.number; i <= endLine.number; i++) {
    const line = view.state.doc.line(i);
    changes.push({ from: line.from, insert: '\t' });
  }

  // WHY: cursor preservation goes through preserveCursorFromLineEnd, NOT
  // arithmetic on `from`/`to`. End-of-line cursors must stay at end-of-line so
  // they never land on a Decoration.replace boundary (CheckboxWidget).  Why: end-of-line cursors must stay at end-of-line; arithmetic on from/to would land them on a Decoration.replace boundary, so preserveCursorFromLineEnd is used instead.
  view.dispatch({ changes, selection: preserveCursorFromLineEnd(view.state, changes, sel) });
  return true;
}

/** Remove leading tab or up to 4 spaces from each line in the selection.
 *  At level 0 (no leading whitespace) collapses extra spaces after list
 *  markers so that repeated cmd+[ presses text flush against the marker. */
function outdentLines(view: EditorView): boolean {
  const sel = view.state.selection.main;
  const startLine = view.state.doc.lineAt(sel.from);
  const endLine = view.state.doc.lineAt(sel.to);
  const changes: { from: number; to: number }[] = [];

  for (let i = startLine.number; i <= endLine.number; i++) {
    const line = view.state.doc.line(i);
    const indentMatch = line.text.match(/^(\t| {1,4})/);
    if (indentMatch) {
      changes.push({ from: line.from, to: line.from + indentMatch[0].length });
    } else {
      const { indentLen, content } = stripIndent(line.text);
      const pos = line.from + indentLen;
      const m =
        content.match(/^([-*+] \[[ xX]\])(  +)/) ||
        content.match(/^([-*+])(  +)/) ||
        content.match(/^(\d+\.)(  +)/);
      if (m) {
        changes.push({ from: pos + m[1].length + 1, to: pos + m[1].length + m[2].length });
      }
    }
  }

  if (changes.length === 0) return true;
  view.dispatch({
    changes,
    selection: preserveCursorFromLineEnd(view.state, changes, sel),
  });
  return true;
}

/** Wrap selection in fenced code block, or unwrap if already fenced. */
function insertCodeBlock(view: EditorView): boolean {
  const { from, to, empty } = view.state.selection.main;

  if (empty) {
    // If cursor is between fence lines, unwrap the empty block
    const curLine = view.state.doc.lineAt(from);
    if (curLine.number > 1 && curLine.number < view.state.doc.lines) {
      const lineBefore = view.state.doc.line(curLine.number - 1);
      const lineAfter = view.state.doc.line(curLine.number + 1);
      if (lineBefore.text.startsWith('```') && lineAfter.text.startsWith('```')) {
        view.dispatch({
          changes: { from: lineBefore.from, to: lineAfter.to },
        });
        return true;
      }
    }
    const insert = '```\n\n```';
    view.dispatch({
      changes: { from, insert },
      selection: { anchor: from + 4 },
    });
    return true;
  }

  // Check if the selection is already wrapped in fence lines — unwrap
  const startLine = view.state.doc.lineAt(from);
  const endLine = view.state.doc.lineAt(to);
  if (startLine.number > 1 && endLine.number < view.state.doc.lines) {
    const lineBefore = view.state.doc.line(startLine.number - 1);
    const lineAfter = view.state.doc.line(endLine.number + 1);
    if (lineBefore.text.startsWith('```') && lineAfter.text.startsWith('```')) {
      const selected = view.state.sliceDoc(from, to);
      view.dispatch({
        changes: [
          { from: lineBefore.from, to: startLine.from },   // remove "```\n"
          { from: endLine.to, to: lineAfter.to },           // remove "\n```"
        ],
        selection: { anchor: lineBefore.from, head: lineBefore.from + selected.length },
      });
      return true;
    }
  }

  const selected = view.state.sliceDoc(from, to);
  const insert = '```\n' + selected + '\n```';
  view.dispatch({
    changes: { from, to, insert },
    selection: { anchor: from + 4, head: from + 4 + selected.length },
  });
  return true;
}

/** Strip inline markdown markers and heading prefixes from selection or current line. */
function clearFormatting(view: EditorView): boolean {
  let { from, to } = view.state.selection.main;
  const { empty } = view.state.selection.main;

  // No selection — operate on the current line
  if (empty) {
    const line = view.state.doc.lineAt(from);
    from = line.from;
    to = line.to;
  }

  if (from === to) return true;

  let text = view.state.sliceDoc(from, to);

  // Strip inline markers
  const inlinePatterns: [RegExp, string][] = [
    [/\*\*(.+?)\*\*/g, '$1'],
    [/\*(.+?)\*/g, '$1'],
    [/~~(.+?)~~/g, '$1'],
    [/`(.+?)`/g, '$1'],
  ];
  for (const [pattern, replacement] of inlinePatterns) {
    text = text.replace(pattern, replacement);
  }

  // Strip heading prefixes on each line
  text = text.replace(/^#{1,6}\s/gm, '');

  if (text === view.state.sliceDoc(from, to)) return true;

  view.dispatch({
    changes: { from, to, insert: text },
    selection: { anchor: from, head: from + text.length },
  });
  return true;
}

/** Toggle blockquote prefix on each line in the selection or current line. */
function toggleQuote(view: EditorView): boolean {
  const { from, to, empty } = view.state.selection.main;
  const startLine = view.state.doc.lineAt(from);
  const endLine = empty ? startLine : view.state.doc.lineAt(to);

  // Check if all lines already have quote prefix
  let allQuoted = true;
  for (let i = startLine.number; i <= endLine.number; i++) {
    if (!view.state.doc.line(i).text.startsWith('> ')) {
      allQuoted = false;
      break;
    }
  }

  const changes: { from: number; to: number; insert: string }[] = [];
  for (let i = startLine.number; i <= endLine.number; i++) {
    const line = view.state.doc.line(i);
    if (allQuoted) {
      changes.push({ from: line.from, to: line.from + 2, insert: '' });
    } else {
      changes.push({ from: line.from, to: line.from, insert: '> ' });
    }
  }

  view.dispatch({ changes });
  return true;
}

/** Wrap selected text as a markdown link and show link suggestions. */
function createLink(view: EditorView): boolean {
  const { from, to, empty } = view.state.selection.main;
  if (empty) return false;

  const selectedText = view.state.sliceDoc(from, to);
  const linkText = `[${selectedText}]()`;
  const cursorPos = from + linkText.length - 1;

  view.dispatch({
    changes: { from, to, insert: linkText },
    selection: { anchor: cursorPos },
  });

  const coords = view.coordsAtPos(cursorPos);
  emit('show-link-suggestions', {
    pos: cursorPos, coords: coords ? { top: coords.bottom, lineTop: coords.top, left: coords.left } : null, editorView: view,
  });
  return true;
}

/** Create a note-chat session from selected text and insert link. */
function createNote(view: EditorView): boolean {
  const { from, to, empty } = view.state.selection.main;
  if (empty) return false;

  const { currentProject, currentDocument, currentReference } = useAppStore.getState();
  if (!currentProject || !currentDocument) return false;

  // Panel quick preview (decision A): the focused editor renders the previewed
  // reference and notes are document-scoped there — refuse BEFORE inserting the
  // note: link, with a toast (no silent degradation). Split/center keep notes on
  // the reference: the mode projection, not the column role, decides.
  if (getFocusedIsReference() && !refIsScope(useUIStore.getState().getRefOpenMode(currentDocument.document_id))) {
    useAppStore.getState().showToast(t('noteUnavailableInPreview'), 'info');
    return false;
  }

  const selectedText = view.state.sliceDoc(from, to);
  const tempId = `temp:${Date.now()}`;
  const linkText = `[${selectedText}](note:${tempId})`;

  let anchorRelStart: string | undefined;
  let anchorRelEnd: string | undefined;
  const handle = getActiveHandle();
  if (handle?.ytext) {
    const relStart = Y.createRelativePositionFromTypeIndex(handle.ytext, from);
    const relEnd = Y.createRelativePositionFromTypeIndex(handle.ytext, to);
    anchorRelStart = JSON.stringify(relStart);
    anchorRelEnd = JSON.stringify(relEnd);
  }

  view.dispatch({
    changes: { from, to, insert: linkText },
    selection: { anchor: from + linkText.length },
  });

  const currentTab = useUIStore.getState().getActiveDocState().rightPanelTab;
  useNoteStore.getState().setPreviousRightTab(currentTab);

  emit('open-notes', {});

  // WHY: note parent follows the focused editor's CONTENT TYPE (reference vs document),
  // not the column role. Role describes column geometry and cannot distinguish
  // split-primary (document) from single-editor-primary-rendering-a-reference, which
  // caused notes created in a single editor showing a reference (stayInContext) to parent
  // to the document while their link + anchor pointed into reference text. The correct
  // matrix has four cases — split: doc/ref, single: doc, single-ref-in-center — all
  // resolved by getFocusedIsReference(). The anchor itself already follows the same
  // focused editor via getActiveHandle → claimFocus, so parent id and anchor
  // now derive from one signal, making the mismatch impossible by construction.
  const refId = getFocusedIsReference()
    ? currentReference?.reference_id
    : undefined;
  useNoteChatStore.getState().createNoteSession({
    projectId: currentProject.project_id,
    documentId: currentDocument.document_id,
    referenceId: refId,
    anchor: { offsetStart: from, offsetEnd: to, relStart: anchorRelStart, relEnd: anchorRelEnd },
  }).then(session => {
    const doc = view.state.doc.toString();
    const oldLink = `note:${tempId}`;
    const newLink = `note:${session.session_id}`;
    const pos = doc.indexOf(oldLink);
    if (pos !== -1) {
      view.dispatch({
        changes: { from: pos, to: pos + oldLink.length, insert: newLink },
      });
    }
    useNoteStore.getState().setActiveNoteThreadId(session.session_id, currentDocument.document_id);
    useNoteStore.getState().setConnectedNoteId(session.session_id);
    useNoteChatStore.getState().setPendingInputFocus(true);
  }).catch(() => {
    useAppStore.getState().showToast('Failed to create note', 'error');
    const doc = view.state.doc.toString();
    const linkIndex = doc.indexOf(`(note:${tempId})`);
    if (linkIndex !== -1) {
      const start = doc.lastIndexOf('[', linkIndex);
      if (start !== -1) {
        const end = linkIndex + `(note:${tempId})`.length;
        const originalText = doc.slice(start + 1, doc.indexOf(']', start));
        view.dispatch({
          changes: { from: start, to: end + 1, insert: originalText },
        });
      }
    }
  });
  return true;
}

/** Create a child markdown reference seeded with the selected text, then navigate into it.
 *
 * The source text is left unchanged (no link / wrap inserted). The new reference's body is
 * seeded via `createMarkdownReference`'s `content` field; the collab session loads it on
 * first join. Parent doc resolves to the open reference's document when editing inside one,
 * else the active document. Kept OUT of the action registry (async, toolbar-only). */
export async function createReference(view: EditorView): Promise<boolean> {
  const { from, to, empty } = view.state.selection.main;
  if (empty) return false;

  const { currentProject, currentDocument, currentReference } = useAppStore.getState();
  if (!currentProject) return false;
  // Parent doc: the reference's parent when inside a reference, else the active doc.
  const documentId = currentReference?.document_id ?? currentDocument?.document_id;
  if (!documentId) return false;

  const selectedText = view.state.sliceDoc(from, to);

  try {
    await createMarkdownReference({
      projectId: currentProject.project_id,
      documentId,
      content: selectedText,
    });
    return true;
  } catch {
    useAppStore.getState().showToast(t('failedToCreateReference'), 'error');
    return false;
  }
}

function stripIndent(text: string): { indentLen: number; content: string } {
  let i = 0;
  while (i < text.length && (text[i] === '\t' || text[i] === ' ')) i++;
  return { indentLen: i, content: text.slice(i) };
}

/** Toggle bullet list marker on each line in selection or current line.
 *  Recognizes both `- ` and `* ` bullets when checking/removing; always
 *  inserts `- ` and normalizes existing `* ` to `- ` when adding. */
// ARCH: stripIndent-aware — handles nested (indented) list items by operating at indent offset.
function toggleBulletList(view: EditorView): boolean {
  const sel = view.state.selection.main;
  const startLine = view.state.doc.lineAt(sel.from);
  const endLine = sel.empty ? startLine : view.state.doc.lineAt(sel.to);

  const isCheckbox = (s: string) =>
    s.startsWith('- [ ] ') || s.startsWith('- [x] ') || s.startsWith('- [X] ');
  const isPlainBullet = (s: string) => s.startsWith('- ') || s.startsWith('* ');

  let allBulleted = true;
  for (let i = startLine.number; i <= endLine.number; i++) {
    const { content } = stripIndent(view.state.doc.line(i).text);
    if (content.length === 0) {
      if (sel.empty) allBulleted = false;
      continue;
    }
    if (!isPlainBullet(content) && !isCheckbox(content)) {
      allBulleted = false;
      break;
    }
  }

  const changes: { from: number; to: number; insert: string }[] = [];

  for (let i = startLine.number; i <= endLine.number; i++) {
    const line = view.state.doc.line(i);
    const { indentLen, content } = stripIndent(line.text);
    const pos = line.from + indentLen;

    if (!allBulleted && content.length === 0 && !sel.empty) continue;

    if (allBulleted) {
      if (isCheckbox(content)) {
        changes.push({ from: pos, to: pos + 6, insert: '' });
      } else if (isPlainBullet(content)) {
        changes.push({ from: pos, to: pos + 2, insert: '' });
      }
    } else {
      if (isCheckbox(content)) {
        changes.push({ from: pos, to: pos + 6, insert: '- ' });
      } else if (content.startsWith('* ')) {
        // Normalize `* ` → `- ` (length unchanged).
        changes.push({ from: pos, to: pos + 1, insert: '-' });
      } else if (content.startsWith('- ')) {
        // Already a bullet — no-op for this line.
      } else {
        changes.push({ from: pos, to: pos, insert: '- ' });
      }
    }
  }

  view.dispatch({
    changes,
    selection: snapSelectionToLineEnd(view.state, changes, startLine.number, endLine.number, sel.empty),
  });
  return true;
}

/** Toggle numbered list marker on each line in selection or current line. */
// WHY: stripIndent-aware — handles nested (indented) list items by operating at indent offset.
function toggleNumberedList(view: EditorView): boolean {
  const sel = view.state.selection.main;
  const startLine = view.state.doc.lineAt(sel.from);
  const endLine = sel.empty ? startLine : view.state.doc.lineAt(sel.to);

  const NUM_RE = /^\d+\.\s/;
  let allNumbered = true;
  for (let i = startLine.number; i <= endLine.number; i++) {
    const { content } = stripIndent(view.state.doc.line(i).text);
    if (content.length === 0) {
      if (sel.empty) allNumbered = false;
      continue;
    }
    if (!NUM_RE.test(content)) {
      allNumbered = false;
      break;
    }
  }

  const changes: { from: number; to: number; insert: string }[] = [];
  let counter = 1;

  for (let i = startLine.number; i <= endLine.number; i++) {
    const line = view.state.doc.line(i);
    const { indentLen, content } = stripIndent(line.text);
    const pos = line.from + indentLen;
    const numStr = `${counter}. `;

    if (!allNumbered && content.length === 0 && !sel.empty) continue;

    if (allNumbered) {
      const match = content.match(/^(\d+\.\s)/);
      if (match) {
        changes.push({ from: pos, to: pos + match[1].length, insert: '' });
      }
    } else {
      if (NUM_RE.test(content)) {
        const match = content.match(/^(\d+\.\s)/);
        if (match) changes.push({ from: pos, to: pos + match[1].length, insert: numStr });
      } else if (content.startsWith('- [ ] ') || content.startsWith('- [x] ') || content.startsWith('- [X] ')) {
        changes.push({ from: pos, to: pos + 6, insert: numStr });
      } else if (content.startsWith('- ')) {
        changes.push({ from: pos, to: pos + 2, insert: numStr });
      } else {
        changes.push({ from: pos, to: pos, insert: numStr });
      }
      counter++;
    }
  }

  view.dispatch({
    changes,
    selection: snapSelectionToLineEnd(view.state, changes, startLine.number, endLine.number, sel.empty),
  });
  return true;
}

/** Toggle checkbox list marker on each line in selection or current line. */
// WHY: stripIndent-aware — handles nested (indented) list items by operating at indent offset.
function toggleCheckboxList(view: EditorView): boolean {
  const sel = view.state.selection.main;
  const startLine = view.state.doc.lineAt(sel.from);
  const endLine = sel.empty ? startLine : view.state.doc.lineAt(sel.to);

  const changes: { from: number; to: number; insert: string }[] = [];

  for (let i = startLine.number; i <= endLine.number; i++) {
    const line = view.state.doc.line(i);
    const { indentLen, content } = stripIndent(line.text);
    const pos = line.from + indentLen;

    if (content.length === 0 && !sel.empty) continue;

    if (content.startsWith('- [x] ') || content.startsWith('- [X] ')) {
      changes.push({ from: pos, to: pos + 6, insert: '' });
    } else if (content.startsWith('- [ ] ')) {
      changes.push({ from: pos, to: pos + 6, insert: '- [x] ' });
    } else if (content.startsWith('- ')) {
      changes.push({ from: pos, to: pos + 2, insert: '- [ ] ' });
    } else {
      changes.push({ from: pos, to: pos, insert: '- [ ] ' });
    }
  }

  view.dispatch({
    changes,
    selection: snapSelectionToLineEnd(view.state, changes, startLine.number, endLine.number, sel.empty),
  });
  return true;
}

/** Toggle voice recording: start widget on first press, stop on second. */
function voiceInput(view: EditorView): boolean {
  // Guard: voice input requires full access (editing capability)
  const accessLevel = useAppStore.getState().accessLevel;
  if (accessLevel !== 'full') return false;

  const widgetState = view.state.field(voiceWidgetField);
  const recording = useAppStore.getState().recording;

  if (!widgetState) {
    // No active widget — start recording if not already recording via REC button
    if (recording) return false;
    const pos = view.state.selection.main.head;
    view.dispatch({ effects: voiceWidgetStart.of({ pos, startedAt: Date.now() }) });
    emit('start-recording');
    return true;
  }

  if (widgetState.state === 'recording') {
    // Recording in progress — stop and begin transcription
    view.dispatch({ effects: voiceWidgetRecognizing.of(undefined) });
    emit('stop-recording');
    return true;
  }

  // Already recognizing — no-op
  return true;
}

const LAST_COLOR_KEY = 'lore:highlight-color';
const DEFAULT_COLOR = '#ec883c';

/* Highlight palette — single source for the toolbar swatches, the persisted
 * markup and DEFAULT_COLOR. WHY plain hex and not var(--color-highlight-N): the
 * color string is written INTO the document as `` `#ec883c text` `` inline code
 * and parsed back by the hex regexes in applyColorHighlight and in
 * live-preview/build-structural.ts — a var() string would change the document
 * format and break both parsers (plan frontend-styling-standardization). */
export const HIGHLIGHT_COLORS = [
  '#ec883c',
  '#8ab4ff',
  '#8ab440',
  '#a978d6',
  '#fdd663',
] as const;

export function getLastHighlightColor(): string {
  return localStorage.getItem(LAST_COLOR_KEY) || DEFAULT_COLOR;
}

export function applyColorHighlight(view: EditorView, color: string): boolean {
  const { from, to, empty } = view.state.selection.main;

  const node = findAncestorOfType(view.state, from, 'InlineCode');
  if (node && node.from <= from && node.to >= to) {
    const contentFrom = node.from + 1;
    const contentTo = node.to - 1;
    if (contentTo > contentFrom) {
      const rawContent = view.state.sliceDoc(contentFrom, contentTo).trim();
      const match = /^(#[0-9a-fA-F]{3,8})(?:\s+(.+))?$/.exec(rawContent);
      if (match) {
        const innerText = match[2] ?? match[1];
        const sel = empty
          ? { anchor: node.from + Math.min(from - contentFrom, innerText.length) }
          : { anchor: node.from, head: node.from + innerText.length };
        view.dispatch({
          changes: { from: node.from, to: node.to, insert: innerText },
          selection: sel,
        });
        return true;
      }
    }
    if (empty) return false;
  }

  if (empty) return false;

  const selected = view.state.sliceDoc(from, to);
  const insert = '`' + color + ' ' + selected + '`';
  view.dispatch({
    changes: { from, to, insert },
    selection: { anchor: from + insert.length },
  });
  localStorage.setItem(LAST_COLOR_KEY, color);
  return true;
}

/** Opens the find/replace right-panel tab (mirrors the open-notes emit).
 *
 * WHY: Carries the current editor selection so FindReplacePanel can overwrite
 * the search field on every Cmd+F (fresh open and repeat). An empty selection
 * yields an empty string — the panel always overwrites per the chosen rule. */
function openFind(view: EditorView): boolean {
  const { from, to } = view.state.selection.main;
  const selection = view.state.doc.sliceString(from, to);
  emit('open-find', { selection });
  return true;
}

/** Insert a new editable table block: create the doc-local model + the anchor.
 *
 * see SYSTEM: table-block — the only sanctioned creation entry point. Builds a 2×2 model in
 * the `tables` Yjs subtree and inserts its `![label](table:id)` anchor on its own line.
 * No live collab handle (mount race) → no-op rather than a dangling anchor. */
function insertTable(view: EditorView): boolean {
  const handle = getActiveHandle();
  if (!handle?.ydoc) return false;
  const id = createTable(handle.ydoc, [['', ''], ['', '']]);
  requestTableFocus(id); // focus the first cell when the widget mounts
  const anchor = tableAnchor(t('tableBlockUntitled'), id);
  const { from, to } = view.state.selection.main;
  const lineStart = view.state.doc.lineAt(from).from;
  const atLineStart = from === lineStart;
  const text = (atLineStart ? '' : '\n') + anchor + '\n';
  view.dispatch({
    changes: { from, to, insert: text },
    selection: { anchor: from + text.length },
  });
  return true;
}

/** Action registry — flat lookup dictionary keyed by MarkdownAction. */
export const markdownActionRegistry: Record<MarkdownAction, (view: EditorView) => boolean> = {
  bold:             (view) => toggleInlineMarker(view, '**'),
  italic:           (view) => toggleInlineMarker(view, '*'),
  strikethrough:    (view) => toggleInlineMarker(view, '~~'),
  inlineCode:       (view) => toggleInlineMarker(view, '`'),
  codeBlock:        insertCodeBlock,
  indent:           indentLines,
  outdent:          outdentLines,
  clearFormatting,
  quote:            toggleQuote,
  heading1:         (view) => setHeading(view, 1),
  heading2:         (view) => setHeading(view, 2),
  heading3:         (view) => setHeading(view, 3),
  heading4:         (view) => setHeading(view, 4),
  createLink,
  createNote,
  checkboxList:    toggleCheckboxList,
  bulletList:      toggleBulletList,
  numberedList:    toggleNumberedList,
  voiceInput,
  highlightColor:  (view) => applyColorHighlight(view, getLastHighlightColor()),
  insertTable,
  openFind,
  // see SYSTEM: selection-region-agent — Cmd+J mirrors the SelectionToolbar Bot button.
  // agentAction no-ops on empty selection and for non-full access, so the chord is inert
  // without a selection or for a read-only role.
  workWithSelection: agentAction,
};

/**
 * Collab presence — left-margin gutter bars for remote carets/selections.
 *
 * // SYSTEM: collab-presence — renders remote awareness as a gutter (not inline).
 * // ARCH: awareness is intentionally kept OUT of yCollab; we read it ourselves and
 * //       render presence only as a left gutter + name chips. No inline remote
 * //       carets in the document body (product decision).
 */

import { EditorView } from '@codemirror/view';
import type { Extension } from '@codemirror/state';
import * as Y from 'yjs';
import type { Awareness } from 'y-protocols/awareness';
import { remotePresenceField, presencePlugin } from './presence-state';
import { presenceDecorations } from './presence-decorations';

// The bar hugs the text: a thin stripe just left of the line, drawn via ::before so
// it never shifts the text. Lives inside .cm-content, so it tracks the centered column.
const presenceTheme = EditorView.theme({
  '.cm-presence-line': { position: 'relative' },
  '.cm-presence-line::before': {
    content: '""',
    position: 'absolute',
    left: '-8px',
    top: '0',
    bottom: '0',
    width: '3px',
    background: 'var(--cm-presence-bg)',
    pointerEvents: 'none',
  },
});

export function collabPresence(
  awareness: Awareness,
  ytext: Y.Text,
  ydoc: Y.Doc,
  onSelectionChange?: (from: number, to: number) => void,
): Extension[] {
  return [
    remotePresenceField,
    presencePlugin(awareness, ytext, ydoc, onSelectionChange),
    presenceDecorations,
    presenceTheme,
  ];
}

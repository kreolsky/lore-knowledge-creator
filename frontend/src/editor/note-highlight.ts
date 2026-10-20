/**
 * Note anchor highlight — a mark over each note's anchored fragment, re-derived from
 * the note's anchor; the text itself carries nothing.
 *
 * Same shape as region-highlight: the StateField holds no state of its own and asks a
 * facet-provided resolver for the ranges to paint. The resolver owns the source — the
 * live editor resolves each note's Yjs RelativePosition pair against the editor's own
 * ydoc (note-anchor-wire.ts); a surface without a Y.Doc can hand ready offsets instead.
 *
 * ARCH: cache gate — re-resolution runs only on a doc change (the anchors may have
 * moved, or a pending anchor's item arrived) or on a `notesChanged` annotation (the
 * note list changed without a CM6 transaction). Selection/viewport/focus dispatches
 * return the cached set.
 */
import { Annotation, Facet, StateField, type EditorState, type Extension } from '@codemirror/state';
import { EditorView, Decoration, type DecorationSet } from '@codemirror/view';
import { openNoteThread } from '../components/editor/live-preview/link-types';
import { isPanelPreviewView, noteUnavailableInPreviewToast } from './editor-plugins';

export interface NoteAnchorRange {
  id: string;
  from: number;
  to: number;
}

type NoteAnchorResolver = (state: EditorState) => NoteAnchorRange[];

const noteAnchorResolver = Facet.define<NoteAnchorResolver, NoteAnchorResolver | undefined>({
  combine: fns => (fns.length ? fns[fns.length - 1] : undefined),
});

/** Tag a no-op dispatch with this when the note list changed (create/delete/reload). */
export const notesChanged = Annotation.define<boolean>();

interface NoteHighlightValue {
  ranges: NoteAnchorRange[];
  decorations: DecorationSet;
}

function build(state: EditorState): NoteHighlightValue {
  const resolver = state.facet(noteAnchorResolver);
  const length = state.doc.length;
  const ranges = (resolver ? resolver(state) : [])
    .filter(r => r.to > r.from && r.from >= 0 && r.to <= length)
    .sort((a, b) => a.from - b.from || a.to - b.to);
  const decorations = Decoration.set(ranges.map(r =>
    Decoration.mark({ class: 'cm-note-anchor', attributes: { 'data-note-id': r.id } }).range(r.from, r.to),
  ), true);
  return { ranges, decorations };
}

const noteHighlightField = StateField.define<NoteHighlightValue>({
  create: build,
  update: (val, tr) => (tr.docChanged || tr.annotation(notesChanged) ? build(tr.state) : val),
  provide: f => EditorView.decorations.from(f, v => v.decorations),
});

/** The painted range of note `id` in this view, or null (not anchored here / pending / orphan). */
export function findNoteAnchorRange(state: EditorState, id: string): NoteAnchorRange | null {
  return state.field(noteHighlightField, false)?.ranges.find(r => r.id === id) ?? null;
}

// WHY: a plain click on a highlighted fragment opens its note, and the event is not
// consumed — the caret still lands where the user clicked, so editing inside a noted
// fragment is unaffected. A click that ends a selection gesture is not a plain click.
const noteAnchorClick = EditorView.domEventHandlers({
  click: (event, view) => {
    if (event.button !== 0 || event.shiftKey || event.altKey) return false;
    if (!view.state.selection.main.empty) return false;
    const target = (event.target as HTMLElement | null)?.closest?.('.cm-note-anchor');
    const id = target?.getAttribute('data-note-id');
    if (!id) return false;
    if (isPanelPreviewView(view)) noteUnavailableInPreviewToast();
    else openNoteThread(id);
    return false;
  },
});

const noteHighlightTheme = EditorView.baseTheme({
  '.cm-note-anchor': {
    backgroundColor: 'var(--warm-tint)',
    borderBottom: '2px dashed var(--warm-strong)',
    cursor: 'pointer',
  },
  '.cm-note-anchor:hover': { backgroundColor: 'var(--warm-strong)' },
  'html.dark & .cm-note-anchor': { backgroundColor: '#5a4a33', borderBottomColor: '#7a6440' },
});

/** Compose the note-highlight extension with a stable resolver closure. */
export function noteHighlightExtension(resolver: NoteAnchorResolver): Extension {
  return [noteAnchorResolver.of(resolver), noteHighlightField, noteAnchorClick, noteHighlightTheme];
}

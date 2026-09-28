/**
 * Pinned-region live highlight — a background decoration re-derived from the
 * frontend-owned Yjs RelativePosition.
 *
 * // see SYSTEM: selection-region-agent — editor highlight for the active region session.
 *
 * A StateField holds no state of its own: on an update it calls the facet-provided
 * resolver, which re-resolves the active region's RelativePosition pair against the
 * live ytext and returns the UTF-16 [from, to] range (or null). A `Decoration.mark`
 * paints a background over that range.
 *
 * # ARCH: cache gate — re-resolution is gated. The field returns its
 * CACHED DecorationSet when a transaction neither changed the doc nor carries a
 * `regionChanged` annotation, so viewport/focus/selection/no-op dispatches skip the
 * resolve (no localStorage read, no RelativePosition math). Re-resolve only on a
 * doc change (local or remote edit — the anchor may have shifted) or a pin/unpin/
 * switch (the `regionChanged` annotation the Editor tags onto its no-op dispatch).
 *
 * # INVARIANT: the resolver MUST resolve against the SAME ydoc the editor binds (the  Why: the resolver must use the editor's bound ydoc (focused EntityHandle) so offsets reflect CRDT-converged text; the Editor gates it on activeItemId==region.doc_id.
 * focused EntityHandle) so the offsets reflect the CRDT-converged text. The Editor
 * component gates the resolver on its own activeItemId == region.doc_id so only the
 * editor showing the pinned doc highlights.
 */
import { Annotation, Facet, StateField, type EditorState, type Extension } from '@codemirror/state';
import { EditorView, Decoration, type DecorationSet } from '@codemirror/view';

export interface RegionRange {
  from: number;
  to: number;
}

// A facet-provided resolver returning the UTF-16 range to highlight (or null). The
// resolver reads live state (chat-store + localStorage + the focused ydoc) at call
// time, so a single stable closure suffices for the editor's lifetime.
const regionHighlightResolver = Facet.define<() => RegionRange | null, (() => RegionRange | null) | undefined>({
  combine: fns => (fns.length ? fns[fns.length - 1] : undefined),
});

function build(state: EditorState): DecorationSet {
  const resolver = state.facet(regionHighlightResolver);
  if (!resolver) return Decoration.none;
  const range = resolver();
  if (!range || range.to <= range.from) return Decoration.none; // collapsed → no highlight
  try {
    return Decoration.set([
      Decoration.mark({ class: 'cm-region-pin' }).range(range.from, range.to),
    ]);
  } catch {
    return Decoration.none; // range out of bounds mid-edit — next transaction recovers
  }
}

/**
 * Annotation tagged onto a no-op editor dispatch when the active region session
 * changes (pin/unpin/switch). The StateField cache gate re-resolves only on a doc
 * change OR this annotation, so a chat-store active-session change (which fires NO
 * CM6 transaction by itself) still refreshes the highlight.
 */
export const regionChanged = Annotation.define<boolean>();

const regionHighlightField = StateField.define<DecorationSet>({
  create: build,
  update: (val, tr) => {
    // ARCH: cache hit — neither the doc nor the region changed. Skips the
    // resolver (and the localStorage read inside getPendingRegion) on viewport/focus/
    // selection/no-op dispatches, which fire on every cursor move and would otherwise
    // re-resolve the RelativePosition needlessly.
    if (!tr.docChanged && !tr.annotation(regionChanged)) return val;
    return build(tr.state);
  },
  provide: f => EditorView.decorations.from(f),
});

const regionHighlightTheme = EditorView.baseTheme({
  '.cm-region-pin': {
    backgroundColor: 'rgba(138, 180, 255, 0.20)',
    boxShadow: 'inset 0 0 0 1px rgba(138, 180, 255, 0.35)',
  },
});

/** Compose the region-highlight extension with a stable resolver closure. */
export function regionHighlightExtension(
  resolver: () => RegionRange | null,
): Extension {
  return [regionHighlightResolver.of(resolver), regionHighlightField, regionHighlightTheme];
}

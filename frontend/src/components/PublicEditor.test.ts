/**
 * Unit tests for the public /s/:token viewer's CM6 extension stack.
 *
 * SYSTEM: editor (public-viewer variant) — the read-only viewer must NEVER
 * reveal raw markdown (the `**`, list markers, fences) on text selection or
 * caret placement. That reveal is an editing affordance gated by the
 * `revealAtCursor` facet; the public stack overrides it to false.
 */

// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView, type DecorationSet } from '@codemirror/view';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { ensureSyntaxTree } from '@codemirror/language';
import { livePreviewField, revealAtCursor } from './editor/live-preview';
import { buildPublicEditorExtensions } from './PublicEditor';

function createPublicView(doc: string, selection: { anchor: number; head?: number }): EditorView {
  const state = EditorState.create({
    doc,
    selection,
    extensions: [
      markdown({ base: markdownLanguage }),
      ...buildPublicEditorExtensions({ tablesJson: null }),
    ],
  });
  const view = new EditorView({ state, parent: document.createElement('div') });
  // Force synchronous parse so the syntax tree is available for decorations.
  ensureSyntaxTree(view.state, view.state.doc.length, 5000);
  // Trigger a decoration rebuild after the tree is ready.
  view.dispatch({ selection });
  return view;
}

/** Collect Replace decorations (no `class`, i.e. hidden ranges) from
 *  livePreviewField — where buildStructuralDecorations hides EmphasisMark. */
function replaceRangesFromField(view: EditorView, field = livePreviewField): { from: number; to: number }[] {
  const out: { from: number; to: number }[] = [];
  (view.state.field(field) as DecorationSet).between(0, view.state.doc.length, (from, to, dec) => {
    const spec = (dec as unknown as { spec: Record<string, unknown> }).spec;
    if (!('class' in spec)) out.push({ from, to });
  });
  return out;
}

describe('public viewer extension stack — revealAtCursor', () => {
  it('facet is false (no raw-markdown reveal on the /s/:token stack)', () => {
    const view = createPublicView('**bold text**', { anchor: 2, head: 6 });
    expect(view.state.facet(revealAtCursor)).toBe(false);
  });

  it('hides the ** EmphasisMark under a non-empty selection (reported bug)', () => {
    // **bold text** — opening ** at [0,2), closing ** at [11,13).
    const doc = '**bold text**';
    const view = createPublicView(doc, { anchor: 2, head: 6 });
    const reps = replaceRangesFromField(view);
    // A Replace decoration must cover the opening `**` marker range.
    const opensAsterisksCovered = reps.some((r) => r.from <= 0 && r.to >= 2);
    expect(opensAsterisksCovered).toBe(true);
    // And the closing `**` marker range.
    const closesAsterisksCovered = reps.some((r) => r.from <= 11 && r.to >= 13);
    expect(closesAsterisksCovered).toBe(true);
  });
});

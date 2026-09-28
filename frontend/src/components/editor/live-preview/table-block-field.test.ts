/**
 * Tests for tableBlockField atomic ranges — the `![label](table:id)` anchor is
 * replaced by a block widget whose raw text is NEVER shown, so the hidden range
 * must be atomic for cursor motion and deletion (the same contract CheckboxWidget
 * already registers via livePreviewField):
 *   - arrow keys skip the anchor as one unit; the caret never rests strictly
 *     inside it (otherwise it walks invisible anchor text and Backspace mangles
 *     it one char at a time, orphaning the table behind an error plate)
 *   - a Backspace that reaches the anchor removes it WHOLE — a partial fragment
 *     must never survive
 *   - a table as the document's first/last line must not trap the caret at the
 *     doc edge
 *
 * Real CM6 EditorView + markdown parser in jsdom (same harness as
 * live-preview-plugin.test.ts). The mounted widget renders blank (no Yjs handle
 * in this file), which does not affect decoration or caret positions.
 *
 * CM6 contract (pinned against @codemirror/view skipAtomicRanges + @codemirror/commands
 * deleteBy): atomicity uses STRICT containment — positions at the range edges are
 * legal caret stops; a delete whose target lands strictly inside is widened to the
 * whole atom.
 */
// @vitest-environment jsdom
import { describe, it, expect, afterEach } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { ensureSyntaxTree } from '@codemirror/language';
import { cursorCharLeft, cursorCharRight, deleteCharBackward } from '@codemirror/commands';
import { tableBlockField } from './fields';
import { tableAnchor } from './table-block-model';

const ANCHOR = tableAnchor('t', 'abc123');
const views: EditorView[] = [];

function makeView(doc: string, caret: number): EditorView {
  const v = new EditorView({
    state: EditorState.create({
      doc,
      selection: { anchor: caret },
      extensions: [markdown({ base: markdownLanguage }), tableBlockField],
    }),
    parent: document.createElement('div'),
  });
  views.push(v);
  ensureSyntaxTree(v.state, v.state.doc.length, 5000);
  return v;
}

/** The anchor occupies its whole line — that whole line is the block-replace range. */
function anchorRange(view: EditorView): { from: number; to: number } {
  const at = view.state.doc.toString().indexOf(ANCHOR);
  if (at < 0) throw new Error(`anchor not in doc: ${view.state.doc.toString()}`);
  return { from: view.state.doc.lineAt(at).from, to: view.state.doc.lineAt(at).to };
}

const head = (v: EditorView): number => v.state.selection.main.head;
const strictlyInside = (r: { from: number; to: number }, pos: number): boolean =>
  pos > r.from && pos < r.to;

afterEach(() => {
  while (views.length) views.pop()!.destroy();
});

describe('tableBlockField — atomic ranges', () => {
  it('exposes the anchor block range via EditorView.atomicRanges', () => {
    const view = makeView(`x\n${ANCHOR}\ny`, 0);
    const r = anchorRange(view);
    const covered = view.state.facet(EditorView.atomicRanges).some((provider) => {
      let hit = false;
      provider(view).between(r.from, r.to, (from, to) => {
        if (from === r.from && to === r.to) hit = true;
      });
      return hit;
    });
    expect(covered).toBe(true);
  });

  it('Right arrow from the anchor line start skips the whole anchor as one unit', () => {
    const doc = `x\n${ANCHOR}\ny`;
    const at = doc.indexOf(ANCHOR);
    const view = makeView(doc, at);
    cursorCharRight(view);
    // One keypress lands past the ENTIRE anchor (its line end), never on the
    // invisible text inside it.
    expect(head(view)).toBe(at + ANCHOR.length);
  });

  it('motion from a caret placed inside the anchor snaps it out to the edge', () => {
    const doc = `x\n${ANCHOR}\ny`;
    const at = doc.indexOf(ANCHOR);
    const view = makeView(doc, at + 3); // e.g. a caret left inside by programmatic selection
    cursorCharLeft(view);
    expect(strictlyInside(anchorRange(view), head(view))).toBe(false);
  });

  it('Backspace from the anchor line end removes the anchor WHOLE (no partial mangle)', () => {
    const doc = `x\n${ANCHOR}\ny`;
    const lineEnd = doc.indexOf(ANCHOR) + ANCHOR.length;
    const view = makeView(doc, lineEnd);
    deleteCharBackward(view);
    const after = view.state.doc.toString();
    // The whole anchor line goes atomically — no fragment ever survives to orphan
    // the table behind a "missing table" error plate.
    expect(after).toBe('x\n\ny');
  });

  it('Backspace directly after the table joins lines but the anchor text survives', () => {
    const doc = `x\n${ANCHOR}\ny`;
    const view = makeView(doc, doc.indexOf('y'));
    deleteCharBackward(view);
    // The caret sits outside the atom: Backspace deletes only the line join, the
    // anchor text stays intact (pinned — this is the pre-existing boundary behavior).
    expect(view.state.doc.toString()).toContain(ANCHOR);
  });

  it('table as the FIRST line: caret traverses to doc start and end without resting inside', () => {
    const doc = `${ANCHOR}\ny`;
    const r = { from: 0, to: ANCHOR.length };
    const view = makeView(doc, 0);
    for (let i = 0; i < doc.length + 2 && head(view) < doc.length; i++) {
      cursorCharRight(view);
      expect(strictlyInside(r, head(view))).toBe(false);
    }
    expect(head(view)).toBe(doc.length);
    for (let i = 0; i < doc.length + 2 && head(view) > 0; i++) {
      cursorCharLeft(view);
      expect(strictlyInside(r, head(view))).toBe(false);
    }
    expect(head(view)).toBe(0);
  });

  it('table as the LAST line: caret traverses to doc start and end without resting inside', () => {
    const doc = `x\n${ANCHOR}`;
    const view = makeView(doc, 0);
    const r = anchorRange(view);
    for (let i = 0; i < doc.length + 2 && head(view) < doc.length; i++) {
      cursorCharRight(view);
      expect(strictlyInside(r, head(view))).toBe(false);
    }
    expect(head(view)).toBe(doc.length);
    for (let i = 0; i < doc.length + 2 && head(view) > 0; i++) {
      cursorCharLeft(view);
      expect(strictlyInside(r, head(view))).toBe(false);
    }
    expect(head(view)).toBe(0);
  });
});

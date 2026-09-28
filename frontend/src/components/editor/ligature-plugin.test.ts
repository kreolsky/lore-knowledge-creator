/** Unit tests for ligature-plugin — CM6 typographic ligature decorations. */

// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import { EditorState } from '@codemirror/state';
import { Decoration, EditorView } from '@codemirror/view';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { ensureSyntaxTree } from '@codemirror/language';
import { ligaturePlugin, ligatureSubstitutedText, LIGATURE_RULES } from './ligature-plugin';
import { revealAtCursor } from './live-preview';

// ── Helpers ──────────────────────────────────────────────────────────────────

function createView(doc: string, cursor: number): EditorView {
  const state = EditorState.create({
    doc,
    selection: { anchor: cursor },
    extensions: [markdown({ base: markdownLanguage }), ligaturePlugin],
  });
  const view = new EditorView({ state, parent: document.createElement('div') });
  ensureSyntaxTree(view.state, view.state.doc.length, 5000);
  view.dispatch({ selection: { anchor: cursor } });
  return view;
}

function getLigatureDecorations(view: EditorView) {
  const decs: { from: number; to: number; glyph: string }[] = [];
  const plugin = view.plugin(ligaturePlugin);
  if (!plugin) return decs;
  plugin.decorations.between(0, view.state.doc.length, (from, to, dec) => {
    const spec = (dec as unknown as { spec: Record<string, unknown> }).spec;
    const widget = spec.widget as { glyph?: string } | undefined;
    if (widget?.glyph) {
      decs.push({ from, to, glyph: widget.glyph });
    }
  });
  return decs;
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('ligature-plugin', () => {
  it('replaces -> with right arrow', () => {
    const view = createView('hello -> world', 0);
    const decs = getLigatureDecorations(view);
    expect(decs).toContainEqual({ from: 6, to: 8, glyph: '\u2192' });
  });

  it('replaces <- with left arrow', () => {
    const view = createView('hello <- world', 0);
    const decs = getLigatureDecorations(view);
    expect(decs).toContainEqual({ from: 6, to: 8, glyph: '\u2190' });
  });

  it('replaces -- with en-dash', () => {
    const view = createView('hello -- world', 0);
    const decs = getLigatureDecorations(view);
    expect(decs).toContainEqual({ from: 6, to: 8, glyph: '\u2013' });
  });

  it('replaces --- with em-dash (not en-dash + dash)', () => {
    const view = createView('hello --- world', 0);
    const decs = getLigatureDecorations(view);
    // Should be single em-dash, not en-dash
    expect(decs).toContainEqual({ from: 6, to: 9, glyph: '\u2014' });
    expect(decs).not.toContainEqual(expect.objectContaining({ glyph: '\u2013' }));
  });

  it('reveals source when cursor is inside match', () => {
    // Cursor at position 7 is inside `->` at positions 6-8
    const view = createView('hello -> world', 7);
    const decs = getLigatureDecorations(view);
    const arrowDec = decs.find((d) => d.from === 6 && d.to === 8);
    expect(arrowDec).toBeUndefined();
  });

  it('renders multiple ligatures on one line', () => {
    const view = createView('a -> b <- c', 0);
    const decs = getLigatureDecorations(view);
    expect(decs).toHaveLength(2);
    expect(decs).toContainEqual({ from: 2, to: 4, glyph: '\u2192' });
    expect(decs).toContainEqual({ from: 7, to: 9, glyph: '\u2190' });
  });

  it('does not replace inside inline code', () => {
    const view = createView('use `->` operator', 0);
    const decs = getLigatureDecorations(view);
    const arrowDec = decs.find((d) => d.glyph === '\u2192');
    expect(arrowDec).toBeUndefined();
  });

  it('does not replace inside fenced code blocks', () => {
    const view = createView('```\na -> b\n```', 0);
    const decs = getLigatureDecorations(view);
    const arrowDec = decs.find((d) => d.glyph === '\u2192');
    expect(arrowDec).toBeUndefined();
  });

  it('does not replace escaped sequences', () => {
    const view = createView('hello \\-> world', 0);
    const decs = getLigatureDecorations(view);
    const arrowDec = decs.find((d) => d.glyph === '\u2192');
    expect(arrowDec).toBeUndefined();
  });

  it('replaces >= with greater-than-or-equal', () => {
    const view = createView('a >= b', 0);
    const decs = getLigatureDecorations(view);
    expect(decs).toContainEqual({ from: 2, to: 4, glyph: '\u2265' });
  });

  it('replaces <= with less-than-or-equal', () => {
    const view = createView('a <= b', 0);
    const decs = getLigatureDecorations(view);
    expect(decs).toContainEqual({ from: 2, to: 4, glyph: '\u2264' });
  });

  it('replaces (c) with copyright', () => {
    const view = createView('Copyright (c) 2025', 0);
    const decs = getLigatureDecorations(view);
    expect(decs).toContainEqual({ from: 10, to: 13, glyph: '\u00A9' });
  });

  it('replaces (с) with copyright', () => {
    const view = createView('Copyright (с) 2025', 0);
    const decs = getLigatureDecorations(view);
    expect(decs).toContainEqual({ from: 10, to: 13, glyph: '\u00A9' });
  });

  it('does not replace escaped (c)', () => {
    const view = createView('hello \\(c) world', 0);
    const decs = getLigatureDecorations(view);
    const copyDec = decs.find((d) => d.glyph === '\u00A9');
    expect(copyDec).toBeUndefined();
  });

  it('replaces --- as em-dash on own line when cursor at end', () => {
    const view = createView('---', 3);
    const decs = getLigatureDecorations(view);
    expect(decs).toContainEqual({ from: 0, to: 3, glyph: '\u2014' });
  });

  it('reveals --- on own line when cursor is inside', () => {
    const view = createView('---', 1);
    const decs = getLigatureDecorations(view);
    expect(decs).toHaveLength(0);
  });

  it('LIGATURE_RULES is sorted longest-first', () => {
    for (let i = 1; i < LIGATURE_RULES.length; i++) {
      expect(LIGATURE_RULES[i - 1].pattern.length).toBeGreaterThanOrEqual(
        LIGATURE_RULES[i].pattern.length,
      );
    }
  });

  // SYSTEM: ligature — reveal-on-cursor is an editing affordance. A nested read-only
  // transclusion view (revealAtCursor=false) must render a leading ligature at offset 0
  // instead of leaving it raw (its selection defaults to position 0).
  describe('nested view (revealAtCursor=false)', () => {
    function createNestedView(doc: string): EditorView {
      const state = EditorState.create({
        doc,
        selection: { anchor: 0 },
        extensions: [markdown({ base: markdownLanguage }), ligaturePlugin, revealAtCursor.of(false)],
      });
      const view = new EditorView({ state, parent: document.createElement('div') });
      ensureSyntaxTree(view.state, view.state.doc.length, 5000);
      view.dispatch({ selection: { anchor: 0 } });
      return view;
    }

    it('renders a leading ligature at offset 0 even with cursor at 0', () => {
      const view = createNestedView('-> target');
      const decs = getLigatureDecorations(view);
      expect(decs).toContainEqual({ from: 0, to: 2, glyph: '\u2192' });
    });

    it('renders a leading em-dash sequence at offset 0', () => {
      const view = createNestedView('--- text');
      const decs = getLigatureDecorations(view);
      expect(decs).toContainEqual({ from: 0, to: 3, glyph: '\u2014' });
    });
  });
});

// ── ligatureSubstitutedText — copy-side glyph substitution ───────────────────
// Pure counterpart to the on-screen plugin: same rules + exclusions, but NO cursor-reveal
// gate (copy is deterministic). The parity test below is the drift guard between the two
// scans — keep it green whenever buildLigatureDecorations changes.

function makeState(doc: string): EditorState {
  const state = EditorState.create({
    doc,
    extensions: [markdown({ base: markdownLanguage })],
  });
  ensureSyntaxTree(state, state.doc.length, 5000);
  return state;
}

describe('ligatureSubstitutedText', () => {
  it('substitutes -> with right arrow', () => {
    const state = makeState('hello -> world');
    expect(ligatureSubstitutedText(state, 0, state.doc.length)).toBe('hello \u2192 world');
  });

  it('substitutes -- with en-dash', () => {
    const state = makeState('hello -- world');
    expect(ligatureSubstitutedText(state, 0, state.doc.length)).toBe('hello \u2013 world');
  });

  it('substitutes --- with em-dash', () => {
    const state = makeState('hello --- world');
    expect(ligatureSubstitutedText(state, 0, state.doc.length)).toBe('hello \u2014 world');
  });

  it('substitutes <- with left arrow', () => {
    const state = makeState('a <- b');
    expect(ligatureSubstitutedText(state, 0, state.doc.length)).toBe('a \u2190 b');
  });

  it('substitutes <-> with left-right arrow', () => {
    const state = makeState('a <-> b');
    expect(ligatureSubstitutedText(state, 0, state.doc.length)).toBe('a \u2194 b');
  });

  it('substitutes >= with greater-equal', () => {
    const state = makeState('a >= b');
    expect(ligatureSubstitutedText(state, 0, state.doc.length)).toBe('a \u2265 b');
  });

  it('substitutes <= with less-equal', () => {
    const state = makeState('a <= b');
    expect(ligatureSubstitutedText(state, 0, state.doc.length)).toBe('a \u2264 b');
  });

  it('substitutes (c) with copyright', () => {
    const state = makeState('Copyright (c) 2025');
    expect(ligatureSubstitutedText(state, 0, state.doc.length)).toBe('Copyright \u00A9 2025');
  });

  it('substitutes (cyrillic с) with copyright', () => {
    const state = makeState('Copyright (\u0441) 2025');
    expect(ligatureSubstitutedText(state, 0, state.doc.length)).toBe('Copyright \u00A9 2025');
  });

  it('keeps -> inside inline code', () => {
    const state = makeState('use `->` operator');
    expect(ligatureSubstitutedText(state, 0, state.doc.length)).toBe('use `->` operator');
  });

  it('keeps -> inside fenced code blocks', () => {
    const state = makeState('```\na -> b\n```');
    expect(ligatureSubstitutedText(state, 0, state.doc.length)).toBe('```\na -> b\n```');
  });

  it('keeps a standalone --- horizontal rule as --- (not em-dash)', () => {
    // Copy is deterministic: HR is ALWAYS excluded (no cursor-reveal gate), so the
    // structural --- survives — never becomes — on the clipboard.
    const state = makeState('---');
    expect(ligatureSubstitutedText(state, 0, state.doc.length)).toBe('---');
  });

  it('keeps an escaped \\-> as ->', () => {
    const state = makeState('hello \\-> world');
    expect(ligatureSubstitutedText(state, 0, state.doc.length)).toBe('hello \\-> world');
  });

  it('respects a sub-range selection (only substitutes within [from, to])', () => {
    const state = makeState('a -> b -> c');
    // Range [2, 9) = '-> b ->' — both arrows fully inside; both substitute.
    expect(ligatureSubstitutedText(state, 2, 9)).toBe('\u2192 b \u2192');
  });

  // PARITY (drift guard): with the cursor placed far from every match, the glyphs the
  // on-screen plugin renders must equal the glyphs ligatureSubstitutedText emits over the
  // whole doc. A divergence means the two scans drifted. (HR is excluded by design in the
  // helper; this doc has no HR to avoid that known, intentional asymmetry.)
  it('PARITY: whole-doc glyphs match the live decoration set when cursor is far from matches', () => {
    const doc = 'a -> b\n<->\nc -- d';
    const view = createView(doc, doc.length);
    const decs = getLigatureDecorations(view);

    // Reconstruct the expected string: original doc with each decorated range replaced by
    // its glyph (ranges are non-overlapping and sorted left-to-right by from).
    const sorted = [...decs].sort((a, b) => a.from - b.from);
    let expected = '';
    let pos = 0;
    for (const d of sorted) {
      expected += doc.slice(pos, d.from) + d.glyph;
      pos = d.to;
    }
    expected += doc.slice(pos);

    const state = makeState(doc);
    expect(ligatureSubstitutedText(state, 0, doc.length)).toBe(expected);
  });
});

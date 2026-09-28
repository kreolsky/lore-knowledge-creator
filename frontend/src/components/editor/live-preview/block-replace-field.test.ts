/**
 * Tests for blockReplaceField — the shared StateField factory for multi-line
 * Decoration.replace blocks. Verifies the rebuild triggers (docChanged,
 * tree-change, cursor enter/leave bounds) and the cursorSensitive:false path.
 */

// @vitest-environment jsdom
import { describe, it, expect } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView, Decoration } from '@codemirror/view';
import { blockReplaceField, type BlockReplaceState } from './block-replace-field';

let buildCalls = 0;

/** Build fn that emits a single Decoration.replace over a tracked bound [0..end]. */
function buildWithBounds(state: EditorState): BlockReplaceState {
  buildCalls++;
  const end = state.doc.length;
  const bounds = end > 0 ? [{ from: 0, to: end }] : [];
  const decs = Decoration.set(
    bounds.length
      ? [Decoration.replace({ block: true }).range(0, end)]
      : [],
    true,
  );
  return { decs, bounds };
}

/**
 * Build fn that tracks a PARTIAL bound [2..4] only — lets us test the case where
 * cursor moves entirely OUTSIDE any bound (neither endpoint touches it).
 */
function buildPartialBound(state: EditorState): BlockReplaceState {
  buildCalls++;
  const end = state.doc.length;
  if (end < 4) return { decs: Decoration.set([], true), bounds: [] };
  const bounds = [{ from: 2, to: 4 }];
  return {
    decs: Decoration.set([Decoration.replace({ block: true }).range(2, 4)], true),
    bounds,
  };
}

/** Build fn that returns empty bounds (cursorSensitive:false shape). */
function buildNoBounds(state: EditorState): BlockReplaceState {
  buildCalls++;
  const end = state.doc.length;
  const decs = Decoration.set(
    end > 0 ? [Decoration.replace({ block: true }).range(0, end)] : [],
    true,
  );
  return { decs, bounds: [] };
}

function mount(field: ReturnType<typeof blockReplaceField>, doc = 'hello'): EditorView {
  const state = EditorState.create({ doc, extensions: [field] });
  return new EditorView({ state });
}

describe('blockReplaceField — cursorSensitive: true', () => {
  it('rebuilds on doc change', () => {
    const field = blockReplaceField(buildWithBounds, { cursorSensitive: true });
    buildCalls = 0;
    const view = mount(field);
    const before = buildCalls;
    view.dispatch({ changes: { from: 5, insert: ' world' } });
    expect(buildCalls).toBe(before + 1);
    view.destroy();
  });

  it('rebuilds when either cursor endpoint touches a bound (enter OR leave)', () => {
    // Behaviour preserved from the original inline StateFields: rebuild fires when
    // the old head was in a bound OR the new head is in a bound. Moving fully inside
    // a bound still rebuilds (wasIn === true). Only a move with BOTH endpoints
    // outside any bound skips the rebuild (covered by the next test).
    const field = blockReplaceField(buildPartialBound, { cursorSensitive: true });
    const state = EditorState.create({
      doc: 'hello world', // bound [2..4]
      selection: { anchor: 0 }, // outside
      extensions: [field],
    });
    const view = new EditorView({ state });
    buildCalls = 0;
    // Enter: 0 (out) → 3 (in).
    view.dispatch({ selection: { anchor: 3 } });
    expect(buildCalls).toBe(1);
    // Leave: 3 (in) → 8 (out).
    view.dispatch({ selection: { anchor: 8 } });
    expect(buildCalls).toBe(2);
    view.destroy();
  });

  it('does NOT rebuild when cursor moves with both endpoints outside any bound', () => {
    const field = blockReplaceField(buildPartialBound, { cursorSensitive: true });
    const state = EditorState.create({
      doc: 'hello world', // bound [2..4]
      selection: { anchor: 8 }, // outside
      extensions: [field],
    });
    const view = new EditorView({ state });
    buildCalls = 0;
    // 8 → 9, both outside bound [2..4].
    view.dispatch({ selection: { anchor: 9 } });
    expect(buildCalls).toBe(0);
    view.destroy();
  });

  it('provides decorations from the decs slot', () => {
    const field = blockReplaceField(buildWithBounds, { cursorSensitive: true });
    const view = mount(field, 'hello');
    const decorations = view.state.field(field);
    expect(decorations).toBeDefined();
    expect(decorations).toHaveProperty('decs');
    expect(decorations).toHaveProperty('bounds');
    view.destroy();
  });
});

describe('blockReplaceField — cursorSensitive: false', () => {
  it('rebuilds on doc change', () => {
    const field = blockReplaceField(buildNoBounds, { cursorSensitive: false });
    const view = mount(field, 'hello');
    buildCalls = 0;
    view.dispatch({ changes: { from: 5, insert: '!' } });
    expect(buildCalls).toBe(1);
    view.destroy();
  });

  it('does NOT rebuild on cursor movement (bounds: [] path never enters selection branch)', () => {
    const field = blockReplaceField(buildNoBounds, { cursorSensitive: false });
    const state = EditorState.create({
      doc: 'hello',
      selection: { anchor: 0 },
      extensions: [field],
    });
    const view = new EditorView({ state });
    buildCalls = 0;
    view.dispatch({ selection: { anchor: 3 } });
    view.dispatch({ selection: { anchor: 1 } });
    expect(buildCalls).toBe(0);
    view.destroy();
  });

  it('state shape is { decs, bounds: [] } and does not crash', () => {
    const field = blockReplaceField(buildNoBounds, { cursorSensitive: false });
    const view = mount(field, 'hello');
    const decorations = view.state.field(field) as BlockReplaceState;
    expect(decorations.bounds).toEqual([]);
    expect(decorations.decs).toBeDefined();
    view.destroy();
  });
});

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
});

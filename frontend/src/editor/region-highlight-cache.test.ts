/**
 * The region-highlight StateField caches its DecorationSet across transactions
 * that don't change the doc or the region, so viewport/focus/no-op dispatches
 * don't re-resolve the RelativePosition.
 *
 * // see SYSTEM: selection-region-agent — the highlight cache-gate.
 */
import { describe, it, expect } from 'vitest';
import { EditorState } from '@codemirror/state';
import { regionHighlightExtension, regionChanged } from './region-highlight';

describe('region-highlight cache gate (D6)', () => {
  it('does NOT call the resolver on a non-docChanged, non-region transaction', () => {
    let calls = 0;
    const ext = regionHighlightExtension(() => {
      calls++;
      return { from: 0, to: 5 };
    });
    let state = EditorState.create({ doc: 'hello world', extensions: ext });
    // Initial create resolves once.
    const initial = calls;
    // A viewport/focus-style no-op transaction (no doc change, no region annotation).
    state = state.update({}).state;
    expect(calls).toBe(initial); // cache hit — resolver not re-invoked
  });

  it('re-resolves on a docChanged transaction', () => {
    let calls = 0;
    const ext = regionHighlightExtension(() => {
      calls++;
      return { from: 0, to: 5 };
    });
    let state = EditorState.create({ doc: 'hello world', extensions: ext });
    const initial = calls;
    state = state.update({ changes: { from: 0, insert: 'A' } }).state;
    expect(calls).toBe(initial + 1);
  });

  it('re-resolves when a regionChanged annotation is present', () => {
    let calls = 0;
    const ext = regionHighlightExtension(() => {
      calls++;
      return { from: 0, to: 5 };
    });
    let state = EditorState.create({ doc: 'hello world', extensions: ext });
    const initial = calls;
    // No doc change, but a region-change annotation (pin/unpin/switch).
    state = state.update({ annotations: regionChanged.of(true) }).state;
    expect(calls).toBe(initial + 1);
  });
});

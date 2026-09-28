/**
 * RelativePosition survival under a surgical edit vs collapse under wholesale.
 *
 * // see SYSTEM: selection-region-agent — the anchor-survival guarantee.
 *
 * The pinned region is tracked by a Yjs RelativePosition pair. It MUST survive the
 * agent's own surgical edit (del slice + insert) so the region stays pinned across
 * the edit. A wholesale edit (del all + insert all) destroys the anchor. This test
 * pins that guarantee at the Yjs (frontend) level — the backend's surgical
 * `apply_external_content_change` produces an update that, applied here, is surgical.
 */
import { describe, it, expect } from 'vitest';
import * as Y from 'yjs';

describe('RelativePosition survival across edits', () => {
  it('survives a surgical edit (del slice + insert) in the unchanged prefix', () => {
    const doc = new Y.Doc();
    const ytext = doc.getText('content');
    ytext.insert(0, 'PREFIX-TARGET-SUFFIX');

    // Anchor at code point 2 (inside the unchanged PREFIX).
    const rel = Y.relativePositionToJSON(Y.createRelativePositionFromTypeIndex(ytext, 2));

    // Surgical: replace "TARGET" (offsets [7,13)) with "X".
    doc.transact(() => {
      ytext.delete(7, 6);
      ytext.insert(7, 'X');
    });

    const abs = Y.createAbsolutePositionFromRelativePosition(
      Y.createRelativePositionFromJSON(rel), doc,
    );
    expect(abs).not.toBeNull();
    expect(abs!.index).toBe(2); // unchanged — anchor survived
    expect(ytext.toString()).toBe('PREFIX-X-SUFFIX');
  });

  it('a relative anchor AFTER the edit point shifts by the delta (tracks the edit)', () => {
    const doc = new Y.Doc();
    const ytext = doc.getText('content');
    ytext.insert(0, 'AAAATARGETZZZZ');
    // Anchor at the 'Z' run start (index 10).
    const rel = Y.relativePositionToJSON(Y.createRelativePositionFromTypeIndex(ytext, 10));

    // Surgical: replace "TARGET" [4,10) with "X" (length -5).
    doc.transact(() => {
      ytext.delete(4, 6);
      ytext.insert(4, 'X');
    });

    const abs = Y.createAbsolutePositionFromRelativePosition(
      Y.createRelativePositionFromJSON(rel), doc,
    );
    expect(abs).not.toBeNull();
    // The anchor followed the left char ( assoc ) — it now sits at index 5 ('Z').
    expect(ytext.toString()[abs!.index]).toBe('Z');
  });

  it('survives a remote surgical update applied to a peer doc', () => {
    // Replica A (the backend) does a surgical edit; the update is applied to peer B
    // (the frontend editor). B's pre-existing RelativePosition must survive.
    const a = new Y.Doc();
    const aText = a.getText('content');
    aText.insert(0, 'head BODY tail');

    // Seed peer B from A's initial state.
    const b = new Y.Doc();
    Y.applyUpdate(b, Y.encodeStateAsUpdate(a));

    // B records a relative anchor at 'B' of BODY (index 5) BEFORE the edit arrives.
    const bText = b.getText('content');
    const rel = Y.relativePositionToJSON(Y.createRelativePositionFromTypeIndex(bText, 5));

    // A does a surgical edit: replace "BODY" [5,9) with "EDITED".
    a.transact(() => {
      aText.delete(5, 4);
      aText.insert(5, 'EDITED');
    });

    // The update reaches B.
    Y.applyUpdate(b, Y.encodeStateAsUpdate(a));

    const abs = Y.createAbsolutePositionFromRelativePosition(
      Y.createRelativePositionFromJSON(rel), b,
    );
    expect(abs).not.toBeNull();
    expect(bText.toString()).toBe('head EDITED tail');
    // The anchor tracked into the replaced span (still a valid position).
    expect(abs!.index).toBeGreaterThanOrEqual(5);
  });
});

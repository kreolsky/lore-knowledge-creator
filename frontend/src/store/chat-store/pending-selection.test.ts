/**
 * resolveRegion status discrimination — the `lost` vs `offline` distinction drives
 * auto-unpin (see SYSTEM: selection-region-agent).
 *
 * A pin over anchors destroyed by a wholesale restore/import ("lost") must be
 * distinguishable from a transient no-ydoc window ("offline"): only the former
 * auto-unpins the session. Ordinary edits and a surviving anchor must stay "ok".
 */
import { describe, it, expect, afterEach } from 'vitest';
import * as Y from 'yjs';
import type { EntityYjsState } from '../../collab/yjs-provider';
import { releaseHandle, publishHandle } from '../../editor/active-editor';
import { resolveRegion, resolveRegionRange, setPendingRegion, clearPendingRegion } from './pending-selection';
import type { PinnedRegion } from '../../types';

const SID = 'sess-1';

function pinRegion(ytext: Y.Text, from: number, to: number): void {
  setPendingRegion(SID, {
    doc_id: 'doc-1',
    relFrom: Y.relativePositionToJSON(Y.createRelativePositionFromTypeIndex(ytext, from)),
    relTo: Y.relativePositionToJSON(Y.createRelativePositionFromTypeIndex(ytext, to)),
  });
}

afterEach(() => {
  clearPendingRegion(SID);
  releaseHandle();
});

describe('resolveRegion status discrimination', () => {
  it('returns "none" when nothing is pinned for the session', () => {
    expect(resolveRegion(SID).status).toBe('none');
  });

  it('returns "offline" when a region is pinned but no ydoc is bound', () => {
    const doc = new Y.Doc();
    const ytext = doc.getText('content');
    ytext.insert(0, 'PREFIX-TARGET-SUFFIX');
    pinRegion(ytext, 7, 13);
    releaseHandle(); // no live editor (cross-device / not yet bound)
    expect(resolveRegion(SID).status).toBe('offline');
  });

  it('returns "ok" with wire offsets when the anchor resolves', () => {
    const doc = new Y.Doc();
    const ytext = doc.getText('content');
    ytext.insert(0, 'PREFIX-TARGET-SUFFIX');
    pinRegion(ytext, 7, 13);
    publishHandle({ ydoc: doc, ytext } as unknown as EntityYjsState);

    const res = resolveRegion(SID);
    expect(res.status).toBe('ok');
    if (res.status !== 'ok') throw new Error('unreachable');
    expect(res.region).toEqual({ doc_id: 'doc-1', from_cp: 7, to_cp: 13, text: 'TARGET' });
  });

  it('a SAME-doc wholesale replace collapses the anchors (not lost) — the existing '
    + 'collapsed-region UX handles it; the pin stays for the user to re-pin/unpin', () => {
    const doc = new Y.Doc();
    const ytext = doc.getText('content');
    ytext.insert(0, 'PREFIX-TARGET-SUFFIX');
    pinRegion(ytext, 7, 13);
    publishHandle({ ydoc: doc, ytext } as unknown as EntityYjsState);

    // Wholesale content replace on the SAME doc/client (checkpoint restore / import):
    // the old items tombstone, both anchors resolve to the boundary (index 0). This is
    // a COLLAPSED region (from==to), NOT a null/lost anchor.
    doc.transact(() => {
      ytext.delete(0, ytext.length);
      ytext.insert(0, 'COMPLETELY-NEW-CONTENT');
    });

    const res = resolveRegion(SID);
    expect(res.status).toBe('ok');
    if (res.status !== 'ok') throw new Error('unreachable');
    expect(res.region.from_cp).toBe(res.region.to_cp); // collapsed
  });

  it('returns "lost" when the anchoring client\'s ops are absent from the live doc '
    + '(post-compaction reload / cross-device divergence / foreign-client state)', () => {
    const authored = new Y.Doc();
    const authoredText = authored.getText('content');
    authoredText.insert(0, 'PREFIX-TARGET-SUFFIX');
    pinRegion(authoredText, 7, 13);

    // The live editor binds a doc that never carried the anchoring client's ops (e.g.
    // the server compacted the ydoc log, dropping the tombstones the anchor needs).
    const live = new Y.Doc();
    const liveText = live.getText('content');
    liveText.insert(0, 'COMPLETELY-NEW-CONTENT');
    publishHandle({ ydoc: live, ytext: liveText } as unknown as EntityYjsState);

    expect(resolveRegion(SID).status).toBe('lost');
  });
});

describe('resolveRegionRange — shared region→UTF-16 range resolver (ghost + materialized)', () => {
  function pin(ytext: Y.Text, from: number, to: number): PinnedRegion {
    return {
      doc_id: 'doc-1',
      relFrom: Y.relativePositionToJSON(Y.createRelativePositionFromTypeIndex(ytext, from)),
      relTo: Y.relativePositionToJSON(Y.createRelativePositionFromTypeIndex(ytext, to)),
    };
  }

  it('returns null when no ydoc is bound (offline / not-yet-bound)', () => {
    const doc = new Y.Doc();
    const ytext = doc.getText('content');
    ytext.insert(0, 'PREFIX-TARGET-SUFFIX');
    const region = pin(ytext, 7, 13);
    releaseHandle();
    expect(resolveRegionRange(region)).toBeNull();
  });

  it('returns the UTF-16 [from, to] range when the anchor resolves', () => {
    const doc = new Y.Doc();
    const ytext = doc.getText('content');
    ytext.insert(0, 'PREFIX-TARGET-SUFFIX');
    const region = pin(ytext, 7, 13);
    publishHandle({ ydoc: doc, ytext } as unknown as EntityYjsState);
    expect(resolveRegionRange(region)).toEqual({ from: 7, to: 13 });
  });

  it('returns null when the anchoring ops are absent from the live doc (lost)', () => {
    const authored = new Y.Doc();
    const authoredText = authored.getText('content');
    authoredText.insert(0, 'PREFIX-TARGET-SUFFIX');
    const region = pin(authoredText, 7, 13);
    const live = new Y.Doc();
    const liveText = live.getText('content');
    liveText.insert(0, 'COMPLETELY-NEW-CONTENT');
    publishHandle({ ydoc: live, ytext: liveText } as unknown as EntityYjsState);
    expect(resolveRegionRange(region)).toBeNull();
  });
});

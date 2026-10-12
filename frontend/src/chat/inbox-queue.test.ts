/** inbox-queue: the snapshot + splice fold that renders dsh's next-turn inbox. */
import { describe, it, expect } from 'vitest';
import {
  EMPTY_FOLD, foldQueue, snapshotFrom, spliceFromFrame, toQueuedMessage,
  withSnapshot, withSplice, type InboxFold,
} from './inbox-queue';

const msg = (id: string, text: string, extra: object[] = []) => ({
  id, role: 'user', source: { kind: 'user' },
  content: [{ type: 'text', text }, ...extra],
});

const splice = (seq: number, start: number, removedCount: number, inserted: unknown[] = [], target = 'next-turn') => ({
  type: 'dsh_event', kind: 'agent/inbox/spliced', seq,
  data: { target, start, removedCount, inserted },
});

function apply(fold: InboxFold, frame: unknown): InboxFold {
  const s = spliceFromFrame(frame);
  return s ? withSplice(fold, s) : fold;
}

describe('inbox-queue', () => {
  it('a chip carries the dsh id, its text blocks and its image count', () => {
    expect(toQueuedMessage(msg('m1', 'hello', [{ type: 'image', attachment: {} }]))).toEqual({
      id: 'm1', text: 'hello', images: 1,
    });
    expect(toQueuedMessage({ content: [] })).toBeNull();
  });

  it('shows nothing until the snapshot seats — a splice alone cannot be placed', () => {
    const fold = apply(EMPTY_FOLD, splice(5, 0, 0, [msg('a', 'A')]));
    expect(foldQueue(fold)).toEqual([]);
    expect(foldQueue(withSnapshot(fold, snapshotFrom([], 4))).map(m => m.id)).toEqual(['a']);
  });

  it('two inserts, then the claim of the head, then a discard of the rest', () => {
    let fold = withSnapshot(EMPTY_FOLD, snapshotFrom([], 10));
    fold = apply(fold, splice(11, 0, 0, [msg('a', 'A')]));
    fold = apply(fold, splice(12, 1, 0, [msg('b', 'B')]));
    expect(foldQueue(fold).map(m => m.text)).toEqual(['A', 'B']);
    fold = apply(fold, splice(13, 0, 1));
    expect(foldQueue(fold).map(m => m.id)).toEqual(['b']);
    fold = apply(fold, splice(14, 0, 1));
    expect(foldQueue(fold)).toEqual([]);
  });

  it('a snapshot drops the splices it already includes and keeps the ones above it', () => {
    let fold = withSnapshot(EMPTY_FOLD, snapshotFrom([], 1));
    fold = apply(fold, splice(2, 0, 0, [msg('a', 'A')]));
    fold = apply(fold, splice(3, 1, 0, [msg('b', 'B')]));
    // The GET read the inbox at seq 2 (only `a`); the seq-3 insert arrived first.
    fold = withSnapshot(fold, snapshotFrom([msg('a', 'A')], 2));
    expect(fold.splices.map(s => s.seq)).toEqual([3]);
    expect(foldQueue(fold).map(m => m.id)).toEqual(['a', 'b']);
  });

  it('a replayed seq is folded once; out-of-order delivery folds in seq order', () => {
    let fold = withSnapshot(EMPTY_FOLD, snapshotFrom([], 0));
    fold = apply(fold, splice(2, 1, 0, [msg('b', 'B')]));
    fold = apply(fold, splice(1, 0, 0, [msg('a', 'A')]));
    fold = apply(fold, splice(1, 0, 0, [msg('a', 'A')]));
    expect(foldQueue(fold).map(m => m.id)).toEqual(['a', 'b']);
  });

  it('a null snapshot seq (no live agent) folds every splice', () => {
    let fold = withSnapshot(EMPTY_FOLD, snapshotFrom(undefined, null));
    fold = apply(fold, splice(40, 0, 0, [msg('a', 'A')]));
    expect(foldQueue(fold).map(m => m.id)).toEqual(['a']);
  });

  it('ignores next-step splices and every other frame', () => {
    expect(spliceFromFrame(splice(3, 0, 0, [msg('s', 'S')], 'next-step'))).toBeNull();
    expect(spliceFromFrame({ type: 'dsh_event', kind: 'turn/start', seq: 3, data: {} })).toBeNull();
    expect(spliceFromFrame({ type: 'dsh_status', status: 'running' })).toBeNull();
  });
});

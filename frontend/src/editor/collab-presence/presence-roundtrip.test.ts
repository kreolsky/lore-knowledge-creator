/**
 * Reproduces the live cursor round-trip headlessly: writer encodes its selection as
 * a Yjs relative position into awareness; reader decodes it and resolves it back to
 * an absolute offset against its own (synced) Y.Doc. Mirrors what presence-state.ts
 * does in the editor, minus CodeMirror.
 *
 * Bug under investigation: the remote bar is stuck on line 1 (offset ~0) and never
 * follows the real caret.
 */
import { describe, it, expect } from 'vitest';
import * as Y from 'yjs';
import { Awareness, encodeAwarenessUpdate, applyAwarenessUpdate } from 'y-protocols/awareness';

function sync(from: Y.Doc, to: Y.Doc) {
  Y.applyUpdate(to, Y.encodeStateAsUpdate(from));
}

function writeCursor(aw: Awareness, ytext: Y.Text, anchor: number, head: number) {
  aw.setLocalStateField('cursor', {
    anchor: Y.relativePositionToJSON(Y.createRelativePositionFromTypeIndex(ytext, anchor)),
    head: Y.relativePositionToJSON(Y.createRelativePositionFromTypeIndex(ytext, head)),
  });
}

function resolve(relJson: unknown, ydoc: Y.Doc): number | null {
  const rel = Y.createRelativePositionFromJSON(relJson);
  const abs = Y.createAbsolutePositionFromRelativePosition(rel, ydoc);
  return abs ? abs.index : null;
}

describe('cursor relative-position round-trip', () => {
  it('resolves a mid-document caret to the same offset on the peer', () => {
    const docA = new Y.Doc();
    const textA = docA.getText('content');
    textA.insert(0, 'Heading line\nSecond\nПривет! Да, занятная тема! И чё?');
    const caret = 25; // somewhere in the third line

    const docB = new Y.Doc();
    sync(docA, docB);

    const awA = new Awareness(docA);
    const awB = new Awareness(docB);
    writeCursor(awA, textA, caret, caret);

    applyAwarenessUpdate(awB, encodeAwarenessUpdate(awA, [awA.clientID]), 'remote');

    const state = awB.getStates().get(awA.clientID)!;
    const cursor = state.cursor as { anchor: unknown; head: unknown };
    expect(resolve(cursor.anchor, docB)).toBe(caret);
    expect(resolve(cursor.head, docB)).toBe(caret);
  });

  it('tracks the caret after the writer edits the document', () => {
    const docA = new Y.Doc();
    const textA = docA.getText('content');
    textA.insert(0, 'Heading\nbody');
    const docB = new Y.Doc();
    sync(docA, docB);

    const awA = new Awareness(docA);
    const awB = new Awareness(docB);

    // Caret at end of "body" (offset 12), then writer types " more" before it moves.
    writeCursor(awA, textA, 12, 12);
    textA.insert(12, ' more'); // now caret logically should be 17 if re-written
    sync(docA, docB);
    writeCursor(awA, textA, 17, 17);

    applyAwarenessUpdate(awB, encodeAwarenessUpdate(awA, [awA.clientID]), 'remote');
    const state = awB.getStates().get(awA.clientID)!;
    const cursor = state.cursor as { anchor: unknown };
    expect(resolve(cursor.anchor, docB)).toBe(17);
  });
});

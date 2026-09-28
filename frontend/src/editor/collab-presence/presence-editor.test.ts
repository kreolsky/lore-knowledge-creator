// @vitest-environment jsdom
/**
 * Drives the presence ViewPlugin inside a real (headless) EditorView to verify that
 * a local selection change is written into awareness as a tracking relative position
 * — the writer half of the live cursor flow.
 */
import { describe, it, expect } from 'vitest';
import { EditorView } from '@codemirror/view';
import { EditorState } from '@codemirror/state';
import * as Y from 'yjs';
import { Awareness, encodeAwarenessUpdate, applyAwarenessUpdate } from 'y-protocols/awareness';
import { presencePlugin, remotePresenceField } from './presence-state';

const nextFrame = () => new Promise<void>(r => requestAnimationFrame(() => r()));

// Recompute is rAF-deferred; under parallel test load a single frame can be missed,
// so poll a few frames until the field is populated.
async function waitForPresence(view: { state: { field: (f: typeof remotePresenceField) => Map<number, unknown> } }) {
  for (let i = 0; i < 8; i++) {
    if (view.state.field(remotePresenceField).size > 0) return;
    await nextFrame();
  }
}

function resolve(relJson: unknown, ydoc: Y.Doc): number | null {
  const abs = Y.createAbsolutePositionFromRelativePosition(Y.createRelativePositionFromJSON(relJson), ydoc);
  return abs ? abs.index : null;
}

describe('presencePlugin (writer)', () => {
  it('writes the moved caret into awareness on selection change', () => {
    const ydoc = new Y.Doc();
    const ytext = ydoc.getText('content');
    ytext.insert(0, 'Heading line\nbody paragraph here');
    const awareness = new Awareness(ydoc);

    const view = new EditorView({
      state: EditorState.create({
        doc: ytext.toString(),
        extensions: [presencePlugin(awareness, ytext, ydoc)],
      }),
      parent: document.body,
    });

    // Move caret into the second line (offset 20).
    view.dispatch({ selection: { anchor: 20 } });

    const cursor = awareness.getLocalState()?.cursor as { anchor: unknown; head: unknown };
    expect(cursor).toBeTruthy();
    expect(resolve(cursor.anchor, ydoc)).toBe(20);

    view.destroy();
  });

  it('places a remote peer cursor on the right line in the presence field', async () => {
    const ydoc = new Y.Doc();
    const ytext = ydoc.getText('content');
    ytext.insert(0, 'Heading\nsecond line\nthird line body'); // line 3 starts at offset 20
    const awareness = new Awareness(ydoc);

    const view = new EditorView({
      state: EditorState.create({
        doc: ytext.toString(),
        extensions: [remotePresenceField, presencePlugin(awareness, ytext, ydoc)],
      }),
      parent: document.body,
    });

    // A synced peer parks its caret on line 3 (offset 25).
    const peerDoc = new Y.Doc();
    Y.applyUpdate(peerDoc, Y.encodeStateAsUpdate(ydoc));
    const peer = new Awareness(peerDoc);
    peer.setLocalStateField('user', { id: 'peer', name: 'Peer', color: '#abcdef' });
    peer.setLocalStateField('cursor', {
      anchor: Y.relativePositionToJSON(Y.createRelativePositionFromTypeIndex(peerDoc.getText('content'), 25)),
      head: Y.relativePositionToJSON(Y.createRelativePositionFromTypeIndex(peerDoc.getText('content'), 25)),
    });
    applyAwarenessUpdate(awareness, encodeAwarenessUpdate(peer, [peer.clientID]), 'remote');

    await waitForPresence(view); // recompute is deferred out of the update cycle

    const line3 = view.state.field(remotePresenceField).get(3);
    expect(line3?.map(u => u.id)).toEqual(['peer']);
    // The stale-frozen-on-line-1 bug would put the peer on line 1, not line 3.
    expect(view.state.field(remotePresenceField).get(1)).toBeUndefined();

    view.destroy();
    peer.destroy();
  });
});

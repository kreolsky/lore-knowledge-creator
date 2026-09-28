import { describe, it, expect } from 'vitest';
import * as Y from 'yjs';
import { Awareness, encodeAwarenessUpdate } from 'y-protocols/awareness';
import { YjsAwarenessAdapter, removeAwarenessStatesForUser } from '../yjs-binding';

const LOCAL = { id: 'me', name: 'Me', color: '#123456' };

describe('YjsAwarenessAdapter', () => {
  it('sets the local user state on construction', () => {
    const doc = new Y.Doc();
    const adapter = new YjsAwarenessAdapter(doc, LOCAL);
    const aw = adapter.getAwareness() as Awareness;
    expect(aw.getLocalState()?.user).toEqual(LOCAL);
    adapter.destroy();
  });

  it('applies a remote awareness update from a peer', () => {
    const doc = new Y.Doc();
    const adapter = new YjsAwarenessAdapter(doc, LOCAL);
    const aw = adapter.getAwareness() as Awareness;

    // Second, independent client produces an update.
    const peerDoc = new Y.Doc();
    const peer = new Awareness(peerDoc);
    peer.setLocalStateField('user', { id: 'peer', name: 'Peer', color: '#abcdef' });
    const payload = encodeAwarenessUpdate(peer, [peer.clientID]);

    adapter.receiveRemote(payload);

    const peerState = aw.getStates().get(peer.clientID);
    expect(peerState?.user).toEqual({ id: 'peer', name: 'Peer', color: '#abcdef' });

    adapter.destroy();
    peer.destroy();
  });

  it('removeAwarenessStatesForUser drops only the named user, keeping others and self', () => {
    const doc = new Y.Doc();
    const adapter = new YjsAwarenessAdapter(doc, LOCAL);
    const aw = adapter.getAwareness() as Awareness;

    const mkPeer = (id: string) => {
      const d = new Y.Doc();
      const p = new Awareness(d);
      p.setLocalStateField('user', { id, name: id, color: '#000000' });
      adapter.receiveRemote(encodeAwarenessUpdate(p, [p.clientID]));
      return p;
    };
    const p1 = mkPeer('peer1');
    const p2 = mkPeer('peer2');

    removeAwarenessStatesForUser(aw, 'peer1');

    expect(aw.getStates().get(p1.clientID)).toBeUndefined();
    expect(aw.getStates().get(p2.clientID)?.user).toMatchObject({ id: 'peer2' });
    expect(aw.getStates().get(aw.clientID)?.user).toEqual(LOCAL);

    adapter.destroy();
    p1.destroy();
    p2.destroy();
  });

  it('removes local state on destroy', () => {
    const doc = new Y.Doc();
    const adapter = new YjsAwarenessAdapter(doc, LOCAL);
    const aw = adapter.getAwareness() as Awareness;
    const clientID = aw.clientID;
    adapter.destroy();
    // After destroy the local state must be gone (peers stop rendering us).
    expect(aw.getStates().get(clientID)).toBeUndefined();
  });
});

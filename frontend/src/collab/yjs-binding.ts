/**
 * Yjs ↔ CodeMirror 6 binding — wires yCollab into the editor (sync only).
 *
 * // ARCH: yCollab is installed for remote-change application ONLY. Awareness is
 * //       deliberately NOT passed to yCollab — no inline remote carets/selection.
 * //       Presence is rendered separately as a left-margin gutter + name chips
 * //       (see editor/collab-presence/). This is a product decision.
 * // SYSTEM: collab-binding — Yjs/CM6 binding via y-codemirror.next
 */

import * as Y from 'yjs';
import { yCollab } from 'y-codemirror.next';
import { Awareness, applyAwarenessUpdate, removeAwarenessStates } from 'y-protocols/awareness';
import type { Extension } from '@codemirror/state';

export interface AwarenessAdapter {
  receiveRemote(payload: Uint8Array): void;
  getAwareness(): Awareness;
  destroy(): void;
}

export interface LocalPresenceUser {
  id: string;
  name: string;
  color: string;
}

export function createYjsExtension(ytext: Y.Text): Extension[] {
  // WHY: awareness arg is null — yCollab stays sync-only, presence is the gutter.
  return [yCollab(ytext, null, { undoManager: false })];
}

/**
 * Real awareness wrapper around y-protocols `Awareness`, bound to one entity's Y.Doc.
 *
 * // INVARIANT: awareness is per-entity, bound to that entity's Y.Doc. Why: relative
 * // cursor positions are only valid against their own ydoc; sharing one Awareness
 * // across entities corrupts positions on document switch.
 */
export class YjsAwarenessAdapter implements AwarenessAdapter {
  private readonly awareness: Awareness;

  constructor(ydoc: Y.Doc, localUser: LocalPresenceUser) {
    this.awareness = new Awareness(ydoc);
    this.awareness.setLocalStateField('user', {
      id: localUser.id,
      name: localUser.name,
      color: localUser.color,
    });
  }

  receiveRemote(payload: Uint8Array): void {
    applyAwarenessUpdate(this.awareness, payload, 'remote');
  }

  getAwareness(): Awareness {
    return this.awareness;
  }

  destroy(): void {
    // Broadcast removal so peers drop our chip/gutter bar, then tear down.
    removeAwarenessStates(this.awareness, [this.awareness.clientID], 'local');
    this.awareness.destroy();
  }
}

/**
 * Drop a remote user's awareness states (all their clients) — used on a `user_left`
 * notification so the gutter bar vanishes together with the chip. Awareness otherwise
 * self-expires only after ~30s, so a hard tab-close would leave a stale bar meanwhile.
 */
export function removeAwarenessStatesForUser(awareness: Awareness, userId: string): void {
  const toRemove: number[] = [];
  awareness.getStates().forEach((state, clientID) => {
    const user = (state as { user?: { id?: string } }).user;
    if (clientID !== awareness.clientID && user?.id === userId) toRemove.push(clientID);
  });
  if (toRemove.length > 0) removeAwarenessStates(awareness, toRemove, 'remote');
}

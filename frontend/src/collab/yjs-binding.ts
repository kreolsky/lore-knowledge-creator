/**
 * Yjs ↔ CodeMirror 6 binding — wires yCollab into the editor (sync + the document-wide undo).
 *
 * // ARCH: yCollab is installed for remote-change application and undo ONLY. Awareness is
 * //       deliberately NOT passed to yCollab — no inline remote carets/selection.
 * //       Presence is rendered separately as a left-margin gutter + name chips
 * //       (see editor/collab-presence/). This is a product decision.
 * // SYSTEM: collab-binding — Yjs/CM6 binding via y-codemirror.next
 */

import * as Y from 'yjs';
import { yCollab, yUndoManagerKeymap } from 'y-codemirror.next';
import { Awareness, applyAwarenessUpdate, removeAwarenessStates } from 'y-protocols/awareness';
import { Prec, type Extension } from '@codemirror/state';
import { EditorView, keymap } from '@codemirror/view';

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

/**
 * Bind an editor to `ytext`, with Ctrl+Z / redo on the entity's document-wide stack
 * (see collab/document-undo.ts). `undoManager` null = a doc with no entity (snapshot
 * preview, public share): sync only, no undo.
 *
 * INVARIANT: one Mod-z press pops exactly ONE stack item. Why: a cell editor lives inside
 * the host's `.cm-content` and both bind the SAME manager; the cell's bindings run at
 * `Prec.highest` and stop propagation, and the host never sees a cell's events (the table
 * widget's `ignoreEvent` is true), so one press cannot undo two steps.
 */
export function createYjsExtension(ytext: Y.Text, undoManager: Y.UndoManager | null): Extension[] {
  // WHY: awareness arg is null — yCollab stays sync-only, presence is the gutter.
  if (!undoManager) return [yCollab(ytext, null, { undoManager: false })];
  return [
    yCollab(ytext, null, { undoManager }),
    Prec.highest(keymap.of(yUndoManagerKeymap.map((b) => ({ ...b, stopPropagation: true })))),
    // A focus change closes the step: text typed in the host and then in a cell (or in two
    // cells) within the capture timeout stays two Ctrl+Z steps.
    EditorView.domEventHandlers({ focus: () => { undoManager.stopCapturing(); return false; } }),
  ];
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

/**
 * Remote-presence state for the collab gutter: maps document lines → the remote
 * users whose caret/selection currently touches that line.
 *
 * Awareness cursors are stored as Yjs *relative* positions so they survive remote
 * edits; we resolve them to absolute offsets, then to line numbers, ourselves
 * (yCollab is kept sync-only — see collab/yjs-binding.ts).
 */

import { StateField, StateEffect } from '@codemirror/state';
import { EditorView, ViewPlugin, type ViewUpdate } from '@codemirror/view';
import * as Y from 'yjs';
import type { Awareness } from 'y-protocols/awareness';

export interface RemoteUser {
  id: string;
  name: string;
  color: string;
}

export interface RemoteCursor {
  user: RemoteUser;
  from: number;
  to: number;
}

/** Pure: group remote cursors by the lines they span (inclusive). */
export function buildPresenceMap(
  cursors: RemoteCursor[],
  lineNumberAt: (offset: number) => number,
): Map<number, RemoteUser[]> {
  const map = new Map<number, RemoteUser[]>();
  for (const { user, from, to } of cursors) {
    const startLine = lineNumberAt(Math.min(from, to));
    const endLine = lineNumberAt(Math.max(from, to));
    for (let line = startLine; line <= endLine; line++) {
      const existing = map.get(line);
      if (existing) existing.push(user);
      else map.set(line, [user]);
    }
  }
  return map;
}

function isRemoteUser(v: unknown): v is RemoteUser {
  return (
    typeof v === 'object' && v !== null &&
    typeof (v as RemoteUser).id === 'string' &&
    typeof (v as RemoteUser).name === 'string' &&
    typeof (v as RemoteUser).color === 'string'
  );
}

/**
 * Pure: turn raw awareness states into resolvable remote cursors.
 * `resolve` converts a stored relative position → absolute offset (or null if it
 * no longer maps into the current document). The local client is excluded.
 */
export function extractRemoteCursors(
  states: Map<number, Record<string, unknown>>,
  localClientID: number,
  resolve: (rel: unknown) => number | null,
): RemoteCursor[] {
  const cursors: RemoteCursor[] = [];
  for (const [clientID, state] of states) {
    if (clientID === localClientID) continue;
    if (!isRemoteUser(state.user)) continue;
    const cursor = state.cursor as { anchor?: unknown; head?: unknown } | undefined;
    if (!cursor) continue;
    const from = resolve(cursor.anchor);
    const to = resolve(cursor.head);
    if (from === null || to === null) continue;
    cursors.push({ user: state.user, from, to });
  }
  return cursors;
}

// ── CM6 wiring ───────────────────────────────────────────────────────────────

export const setPresence = StateEffect.define<Map<number, RemoteUser[]>>();

/** Lines (1-based) → remote users present on them. Read by the gutter marker. */
export const remotePresenceField = StateField.define<Map<number, RemoteUser[]>>({
  create: () => new Map(),
  update(value, tr) {
    for (const effect of tr.effects) {
      if (effect.is(setPresence)) return effect.value;
    }
    return value;
  },
});

function resolveOffset(relJson: unknown, ydoc: Y.Doc): number | null {
  if (relJson == null) return null;
  try {
    const rel = Y.createRelativePositionFromJSON(relJson);
    const abs = Y.createAbsolutePositionFromRelativePosition(rel, ydoc);
    return abs ? abs.index : null;
  } catch {
    return null;
  }
}

function computePresence(view: EditorView, awareness: Awareness, ydoc: Y.Doc) {
  const docLen = view.state.doc.length;
  const cursors = extractRemoteCursors(
    awareness.getStates() as Map<number, Record<string, unknown>>,
    awareness.clientID,
    rel => resolveOffset(rel, ydoc),
  );
  const map = buildPresenceMap(cursors, offset =>
    view.state.doc.lineAt(Math.max(0, Math.min(offset, docLen))).number,
  );
  view.dispatch({ effects: setPresence.of(map) });
}

/**
 * ViewPlugin that (a) recomputes the presence map on every awareness change and on
 * local document edits (offsets shift), and (b) writes the local selection into
 * awareness as a relative position so peers can render our caret.
 */
export function presencePlugin(
  awareness: Awareness,
  ytext: Y.Text,
  ydoc: Y.Doc,
  onSelectionChange?: (from: number, to: number) => void,
) {
  return ViewPlugin.fromClass(
    class {
      private readonly onChange: () => void;
      private rafId: number | null = null;
      private selTimer: ReturnType<typeof setTimeout> | null = null;
      private destroyed = false;

      constructor(private readonly view: EditorView) {
        // WHY: recompute is scheduled (rAF), never dispatched inline. computePresence
        // calls view.dispatch(); doing that synchronously from this constructor or from
        // update() is a re-entrant dispatch inside the CM update cycle — it throws, the
        // plugin dies, and the local cursor stays frozen at offset 0 (peers then resolve
        // every caret to line 1). Deferring the dispatch escapes the update cycle.
        this.onChange = () => this.schedule();
        awareness.on('change', this.onChange);
        this.writeLocal(view); // setLocalStateField is not a CM dispatch — safe inline
        this.schedule();
      }

      private schedule() {
        if (this.rafId !== null) return;
        this.rafId = requestAnimationFrame(() => {
          this.rafId = null;
          if (!this.destroyed) computePresence(this.view, awareness, ydoc);
        });
      }

      update(update: ViewUpdate) {
        // setLocalStateField fires awareness 'change' synchronously → onChange →
        // schedule() (rAF only, no dispatch), so this stays safe inside the update.
        if (update.selectionSet || update.docChanged) this.writeLocal(update.view);
        if (update.docChanged) this.schedule();
        // Report the local selection to the advisory region-lock registry.
        // Debounced so a drag-select or rapid caret moves don't flood the WS. Code-point
        // offsets (CM6 is UTF-16; the backend resolves ranges in code points — offsets
        // coincide for the BMP markdown content we edit).
        if (onSelectionChange && update.selectionSet) {
          this.scheduleSelectionReport();
        }
      }

      private scheduleSelectionReport() {
        if (this.selTimer !== null) clearTimeout(this.selTimer);
        this.selTimer = setTimeout(() => {
          this.selTimer = null;
          if (this.destroyed) return;
          const sel = this.view.state.selection.main;
          onSelectionChange!(sel.from, sel.to);
        }, 120);
      }

      writeLocal(view: EditorView) {
        const sel = view.state.selection.main;
        const anchor = Y.relativePositionToJSON(Y.createRelativePositionFromTypeIndex(ytext, sel.anchor));
        const head = Y.relativePositionToJSON(Y.createRelativePositionFromTypeIndex(ytext, sel.head));
        awareness.setLocalStateField('cursor', { anchor, head });
      }

      destroy() {
        this.destroyed = true;
        if (this.rafId !== null) cancelAnimationFrame(this.rafId);
        if (this.selTimer !== null) clearTimeout(this.selTimer);
        awareness.off('change', this.onChange);
      }
    },
  );
}

/**
 * Per-entity collab EntityHandle registry for non-React, identity-scoped callers
 * (table widget, badge list, panel table ops, paste): THIS entity's ydoc, regardless
 * of which column is focused. In split view the focused handle (getActiveHandle in
 * editor/active-editor) is routinely null or the OTHER column's handle at read time —
 * resolving by entity id is the only correct address for them.
 */
// ARCH: Set/cleared by useEditorCollab's lifecycle on mount/unmount/conn-land
// and on entity switch. Readers MUST tolerate `null` — there's a small window
// between editor mount and collab init where no handle is registered.
// SYSTEM: active-handle-registry — per-entity EntityHandle registry.

import type { EntityYjsState } from './yjs-provider';

// ─── Per-entity registry (identity-scoped consumers) ─────────────────────────

const entityHandles = new Map<string, EntityYjsState>();
type EntityHandleListener = (handle: EntityYjsState | null) => void;
const entityListeners = new Map<string, Set<EntityHandleListener>>();

/**
 * Publish/clear an entity's handle. Written ONLY by useEditorCollab's lifecycle
 * (both columns register their own entity). An identity re-set is a silent no-op
 * so conn-land epoch re-runs do not notify a replace with the same object.
 *
 * `null` clears the entry and notifies subscribers — a stale entry holding a
 * destroyed ydoc (after `leave()`) would throw inside its observers.
 */
export function setEntityHandle(id: string, handle: EntityYjsState | null): void {
  if (handle === null) {
    if (!entityHandles.has(id)) return;
    entityHandles.delete(id);
  } else {
    if (entityHandles.get(id) === handle) return;
    entityHandles.set(id, handle);
  }
  entityListeners.get(id)?.forEach((l) => l(handle));
}

/** Return the handle registered for an entity, or null (mount race / left). */
export function getEntityHandle(id: string): EntityYjsState | null {
  return entityHandles.get(id) ?? null;
}

/**
 * Subscribe to an entity's handle changes. Fires IMMEDIATELY with the current
 * value, then on every set — the late-bind pattern (subscribe → first non-null
 * → capture → unsubscribe) used by the table widget. Returns an unsubscribe fn.
 */
export function subscribeEntityHandle(id: string, cb: EntityHandleListener): () => void {
  let set = entityListeners.get(id);
  if (!set) {
    set = new Set();
    entityListeners.set(id, set);
  }
  set.add(cb);
  cb(getEntityHandle(id));
  return () => {
    set!.delete(cb);
    if (set!.size === 0) entityListeners.delete(id);
  };
}


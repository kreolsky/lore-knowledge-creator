/**
 * The document-wide undo stack — ONE `Y.UndoManager` per joined entity, shared by the host
 * editor, every table cell editor, the table model and Replace-all.
 *
 * ARCH: the manager is created and destroyed by the provider with its `EntityYjsState`
 * (see `YjsProjectProvider.joinEntity`); this module only indexes it by Y.Doc, because the
 * writers that must mark step boundaries (table model ops, Replace-all, a cell editor) hold
 * the doc, not the entity handle. A doc with no entity (snapshot preview, public share,
 * a bare test doc) has no manager: every helper here is a no-op on it.
 */

import * as Y from 'yjs';

const managers = new WeakMap<Y.Doc, Y.UndoManager>();
/** Managers inside an open group (a drag): boundaries are suppressed until it ends. */
const groups = new WeakSet<Y.UndoManager>();

/**
 * Create and index the entity's manager over `content` + `tables`.
 *
 * INVARIANT: `trackedOrigins` is set EXPLICITLY. Why: the Yjs default `new Set([null])`
 * captures every origin-less local write, so internal writes (seeding, reconcile repairs)
 * would land in the user's stack. Remote updates (origin `'remote'`) and the local mirror's
 * replay are never captured; each `yCollab` binding adds its own sync origin itself.
 */
export function createDocumentUndo(doc: Y.Doc, trackedOrigins: readonly unknown[]): Y.UndoManager {
  const manager = new Y.UndoManager([doc.getText('content'), doc.getMap('tables')], {
    trackedOrigins: new Set(trackedOrigins),
  });
  managers.set(doc, manager);
  return manager;
}

/** Drop the entity's manager — called by the provider BEFORE the doc is destroyed. */
export function destroyDocumentUndo(doc: Y.Doc): void {
  managers.get(doc)?.destroy();
  managers.delete(doc);
}

export function documentUndo(doc: Y.Doc): Y.UndoManager | null {
  return managers.get(doc) ?? null;
}

/** Close the current step: the next captured change starts a new one. No-op inside a group. */
export function stopCapturing(doc: Y.Doc): void {
  const manager = managers.get(doc);
  if (manager && !groups.has(manager)) manager.stopCapturing();
}

/**
 * Open a group: every change until `endUndoGroup` is ONE step, however long it pauses
 * (a column drag, the table scale drag). Returns the closer; calling it twice is safe.
 * WHY a raised `captureTimeout` and not a merge by hand: the manager already merges
 * changes inside the timeout — an infinite one for the group's life is exactly "one step".
 */
export function beginUndoGroup(doc: Y.Doc): () => void {
  const manager = managers.get(doc);
  if (!manager) return () => {};
  manager.stopCapturing();
  const timeout = manager.captureTimeout;
  groups.add(manager);
  manager.captureTimeout = Number.POSITIVE_INFINITY;
  let open = true;
  return () => {
    if (!open) return;
    open = false;
    groups.delete(manager);
    manager.captureTimeout = timeout;
    manager.stopCapturing();
  };
}

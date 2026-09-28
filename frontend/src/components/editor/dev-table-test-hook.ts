/**
 * DEV-ONLY repro hook for the table cell click/focus race.
 *
 * Mounted by Editor.tsx behind `import.meta.env.DEV`. Exposes a deterministic
 * surface on `window.__loreTableTest` so e2e specs can poll collab-handle
 * readiness and seed a table without the timing-flaky Shift-Mod-T hotkey path.
 *
 * No consumer today — the three table specs that pinned this API were retired with the
 * rest of the tracked e2e suite. Kept because a table drive is the next thing to need it
 * and re-deriving the seed surface costs more than keeping a DEV-only module; delete it
 * if no drive has used it by the next table task.
 *
 * API:
 *   handleReady()       → boolean (active handle has a synced ydoc)
 *   insert()            → boolean (focus + insertTable on the live view)
 *   seedMatrix(matrix)  → string | null (tableId, or null if no handle/view)
 *   clearDoc()          → boolean (delete the whole document text — e2e hermeticity)
 */
// SYSTEM: dev-table-test-hook — e2e-only deterministic table seed surface

import { t } from '../../i18n';
import { createTable, tableAnchor } from './live-preview/table-block-model';
import { getActiveHandle, getEditorView } from '../../editor/active-editor';
import { markdownActionRegistry } from './markdown-actions';

export type LoreTableTestApi = {
  handleReady: () => boolean;
  insert: () => boolean;
  seedMatrix: (matrix: string[][]) => string | null;
  clearDoc: () => boolean;
};

declare global {
  interface Window {
    __loreTableTest?: LoreTableTestApi;
  }
}

/** Install the dev-only `window.__loreTableTest` surface. No-op outside DEV. */
export function mountTableTestHook(): void {
  if (!import.meta.env.DEV) return;
  window.__loreTableTest = {
    handleReady: () => getActiveHandle()?.ydoc != null,
    insert: () => {
      const v = getEditorView();
      if (!v) return false;
      v.focus();
      return markdownActionRegistry.insertTable(v);
    },
    // Seed a table directly from a cell-text matrix (bypasses typing/focus timing on the
    // e2e stand). Returns the tableId, or null if no handle/view. Used by observation specs
    // that need a guaranteed shape (e.g. a tall multi-line row) without keyboard focus.
    seedMatrix: (matrix: string[][]): string | null => {
      const handle = getActiveHandle();
      const v = getEditorView();
      if (!handle?.ydoc || !v) return null;
      const id = createTable(handle.ydoc, matrix);
      const anchor = tableAnchor(t('tableBlockUntitled'), id);
      v.dispatch({ changes: { from: v.state.doc.length, insert: anchor } });
      return id;
    },
    // Wipe the whole document text so a spec starts from a hermetic slate. Why:
    // the scratch doc is reused across runs and accumulates tables from failed
    // runs; specs that assert on `.cm-table-block` positions/order read that
    // history. Deleting through the live view goes through the ydoc (CRDT-safe).
    // The `tables` map is cleared TOO: block widgets render from the map (the
    // text anchors alone do not drive them), so wiping only the text would leave
    // every historical table mounted.
    clearDoc: (): boolean => {
      const handle = getActiveHandle();
      const v = getEditorView();
      if (!v) return false;
      v.dispatch({ changes: { from: 0, to: v.state.doc.length } });
      if (handle?.ydoc) {
        const tables = handle.ydoc.getMap('tables');
        for (const key of Array.from(tables.keys())) tables.delete(key);
      }
      return true;
    },
  };
}

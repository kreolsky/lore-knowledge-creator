/**
 * Reference ids deleted during this page session — the chat's generated-image
 * plates hide them on every mount.
 *
 * Standalone store: the fact is cross-surface (a delete from the chat lightbox,
 * the References panel or another tab) and must outlive the chat's components,
 * which unmount on every tab switch. A reload needs none of it — the server's
 * reload mint already drops deleted references.
 */

import { create } from 'zustand';

interface DeletedRefIdsState {
  ids: ReadonlySet<string>;
  add: (ids: readonly string[]) => void;
  /** Undo an optimistic add whose delete request failed. */
  remove: (id: string) => void;
}

export const useDeletedRefIds = create<DeletedRefIdsState>(set => ({
  // WHY: the set lives as long as the page, not as long as a chat plate — a
  // plate holding its own removed-set forgot the deletes on a tab switch and
  // showed the deleted images again until a reload.
  ids: new Set(),
  add: ids => set(s => {
    const fresh = ids.filter(id => !s.ids.has(id));
    return fresh.length ? { ids: new Set([...s.ids, ...fresh]) } : s;
  }),
  remove: id => set(s => {
    if (!s.ids.has(id)) return s;
    const next = new Set(s.ids);
    next.delete(id);
    return { ids: next };
  }),
}));

/**
 * refDragAdapter — the reference-card half of the sibling-drag seam
 * (useSiblingDragReorder). Group = the ref's HOST document (data-ref-group on
 * RefCard); archived and unhosted cards carry no group attribute, so they are
 * neither draggable nor drop targets. Commits to the same
 * PATCH /documents/{id}/reorder as the tree (the route serves both kinds).
 */

import { useAppStore } from '../../store/app-store';
import { t } from '../../i18n';
import { patchSiblingReorder, type DragSibling, type SiblingDragAdapter } from '../../hooks/useSiblingDragReorder';
import type { Reference } from '../../types';

function orderedRefSiblings(refs: Reference[], groupId: string | null, selfId: string): DragSibling[] {
  return refs
    .filter(r => r.archived !== true && (r.document_id ?? null) === groupId && r.reference_id !== selfId)
    .map(r => ({ id: r.reference_id, sortKey: r.sort_key ?? undefined }))
    .sort((a, b) => {
      const ka = a.sortKey ?? '';
      const kb = b.sortKey ?? '';
      if (ka !== kb) return ka < kb ? -1 : 1;
      return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    });
}

export const refDragAdapter: SiblingDragAdapter = {
  rowIdAttr: 'data-ref-id',
  groupAttr: 'data-ref-group',
  orderedSiblings: (groupId, selfId) =>
    orderedRefSiblings(useAppStore.getState().references, groupId, selfId),
  // Don't hijack RefCard action buttons or the inline-rename input.
  ignoreTarget: (el) => !!(el.closest('button') || el.closest('input')),
  commit: (id, afterId) => {
    const refs = useAppStore.getState().references;
    const self = refs.find(r => r.reference_id === id);
    const anchor = afterId ? refs.find(r => r.reference_id === afterId) : undefined;
    void patchSiblingReorder(
      id,
      afterId,
      self?.sort_key ?? undefined,
      anchor?.sort_key ?? undefined,
      // placeReference (not updateReference): the optimistic key must re-place
      // the card inside its group run for instant feedback.
      (key) => useAppStore.getState().placeReference(id, { sort_key: key }),
      'Failed to reorder reference',
      t('failedToReorderReference'),
    );
  },
};

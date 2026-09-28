/**
 * Pure helpers for first-circle cascade selection in the chat content picker.
 *
 * Selection is the single source of truth. Two symmetric operations:
 *   add(parent, links)      -> selection ∪ ({parent} ∪ links)
 *   subtract(parent, links) -> selection \ ({parent} ∪ links)
 *
 * No claim arithmetic, no "added by cascade" memory. Removing X always removes
 * X and X's first circle, regardless of whether the children are also linked
 * from another selected parent. Re-selecting a parent re-merges its first
 * circle. See lessons/2026-05-17-chat-context-pipeline.md.
 */

export interface FirstCircle {
  document_ids: string[];
  reference_ids: string[];
}

export interface SelectionState {
  docIds: string[];
  refIds: string[];
}

/** Add `targetId` and merge its first-circle into selection. Idempotent. */
export function addWithCascade(
  selection: SelectionState,
  targetKind: 'doc' | 'ref',
  targetId: string,
  links: FirstCircle,
): SelectionState {
  const docs = new Set(selection.docIds);
  const refs = new Set(selection.refIds);
  if (targetKind === 'doc') docs.add(targetId);
  else refs.add(targetId);
  links.document_ids.forEach(id => docs.add(id));
  links.reference_ids.forEach(id => refs.add(id));
  return { docIds: [...docs], refIds: [...refs] };
}

/**
 * Set of ids (docs/refs) that appear as first-circle of any selected parent in
 * `linkCache`. Purely for the Link2 UI indicator in the picker — has NO effect
 * on selection behavior. Items not in `linkCache` simply don't contribute.
 */
export function computeClaimed(
  selectedKeys: Iterable<string>,
  linkCache: ReadonlyMap<string, FirstCircle>,
): { docs: Set<string>; refs: Set<string> } {
  const docs = new Set<string>();
  const refs = new Set<string>();
  for (const key of selectedKeys) {
    const links = linkCache.get(key);
    if (!links) continue;
    links.document_ids.forEach(id => docs.add(id));
    links.reference_ids.forEach(id => refs.add(id));
  }
  return { docs, refs };
}

/** Remove `targetId` and every id in its first-circle from selection. Idempotent. */
export function subtractWithCascade(
  selection: SelectionState,
  targetKind: 'doc' | 'ref',
  targetId: string,
  links: FirstCircle,
): SelectionState {
  const docs = new Set(selection.docIds);
  const refs = new Set(selection.refIds);
  if (targetKind === 'doc') docs.delete(targetId);
  else refs.delete(targetId);
  links.document_ids.forEach(id => docs.delete(id));
  links.reference_ids.forEach(id => refs.delete(id));
  return { docIds: [...docs], refIds: [...refs] };
}

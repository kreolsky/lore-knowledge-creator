/**
 * Helpers for routing unified context_ids into doc/ref split at presentation time.
 *
 * // SYSTEM: chat-session-helpers — wire-format <-> internal ChatContext bridge
 * // ARCH: Spec §5 — context_ids is a single wire field; ContentPicker tabs need
 * //       the split (docs vs refs). The split lives at presentation time only;
 * //       the canonical store is unified.
 */

export interface SplitContextIds {
  docIds: string[];
  refIds: string[];
}

export function splitContextIds(ids: string[], refIdSet: Set<string>): SplitContextIds {
  const docIds: string[] = [];
  const refIds: string[] = [];
  for (const id of ids) {
    (refIdSet.has(id) ? refIds : docIds).push(id);
  }
  return { docIds, refIds };
}

/** Chat-session list ordering: recency DESC or per-document proximity. */

import { useMemo } from 'react';
import { useChatStore } from '../../store/chat-store';
import { useUIStore } from '../../store/ui-store';
import { useAppStore } from '../../store/app-store';
import { buildParentMap, treeDistance, PROJECT_ROOT } from '../../utils/document-sort';
import type { ChatSession } from '../../types';

/**
 * Returns ALL sessions ordered for the chat empty-list. Two modes:
 *
 * - Recency (default): last USER message time DESC. The chat-store's optimistic
 *   send already sets last_message_at to the user-msg `now`, matching the backend
 *   aggregate (max user-message created_at) — so a SEND instantly reorders and
 *   there is no flicker on reload.
 * - Proximity (per-doc opt-in): tree-distance from the OPEN document ASC, tiebreak
 *   last-user-msg DESC. A reference-scoped chat's document_id is its ref-doc (its
 *   parent in the tree), so it ranks by its owning branch — consistent with the
 *   ChatRow `ref:` label. Doc-less sessions rank via PROJECT_ROOT (finite).
 *
 * ARCH: the recency fallback is
 * `created_at`, NEVER `updated_at`. Why: auto_title / update_session bump
 * updated_at, which would reorder the list on enter→leave (the "date jump").
 * created_at is immutable, so the list position is stable until a real user
 * message lands. ISO string compare is safe (same ISO-8601 source); Array sort
 * is stable so equal timestamps keep prior order.
 *
 * DEBT: list shows newest 200; server pagination pending — Why deferred: no user
 * near the cap yet. ChatEmptyList renders this same set as one scrolling list.
 */
export function useSortedSessions() {
  const sessions = useChatStore(s => s.sessions);
  const currentDocId = useAppStore(s => s.currentDocument?.document_id) ?? null;
  const documents = useAppStore(s => s.documents);
  const sortByProximity = useUIStore(s => s.getChatSortByProximity(currentDocId));

  return useMemo(() => {
    // Last user message, falling back to the immutable created_at.
    const recencyOf = (s: ChatSession): string => s.last_message_at ?? s.created_at;
    const byRecencyDesc = (a: ChatSession, b: ChatSession) =>
      recencyOf(b).localeCompare(recencyOf(a));

    // Proximity sort only when opted in AND an anchor document is open.
    if (sortByProximity && currentDocId) {
      const parentMap = buildParentMap(documents);
      const byProximity = (a: ChatSession, b: ChatSession) => {
        const distA = treeDistance(currentDocId, a.document_id ?? PROJECT_ROOT, parentMap);
        const distB = treeDistance(currentDocId, b.document_id ?? PROJECT_ROOT, parentMap);
        if (distA !== distB) return distA - distB;
        // Tiebreak: recency DESC within a proximity tier.
        return recencyOf(b).localeCompare(recencyOf(a));
      };
      return [...sessions].sort(byProximity);
    }

    return [...sessions].sort(byRecencyDesc);
  }, [sessions, sortByProximity, currentDocId, documents]);
}

// Minimum query length before the ghost-header search narrows the list.
export const CHAT_LIST_FILTER_MIN = 3;

/**
 * Title-only, case-insensitive substring filter for the chat list. Returns the
 * SAME array when the query is inactive (null or shorter than the minimum) so
 * callers can tell "filtered to nothing" from "no chats" by identity.
 */
// WHY fold both sides to a diacritic-free lowercase form: a typed "Ё" can arrive
// decomposed (Е + U+0308) while the stored title holds the precomposed U+0401,
// and a lowercase-only compare missed "Ёжи" in "ёжика". NFD + stripping combining
// marks also folds ё→е, so "ежик" finds "ёжик" either way. Latin Ë/ë (U+00CB/
// U+00EB — a Mac Option+U keystroke) is a visual twin of ё and is mapped to it
// first; other Latin look-alikes are a wrong-layout typo, not folded.
const fold = (v: string): string =>
  v.replace(/[\u00cb\u00eb]/g, 'ё').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();

export function filterSessionsByTitle(sessions: ChatSession[], query: string | null): ChatSession[] {
  if (!query) return sessions;
  const q = fold(query.trim());
  if (q.length < CHAT_LIST_FILTER_MIN) return sessions;
  return sessions.filter(s => fold(s.title).includes(q));
}

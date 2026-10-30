/** Chat-session list ordering: recency DESC or per-document proximity. */

import { useMemo } from 'react';
import { useChatStore } from '../../store/chat-store';
import { useUIStore } from '../../store/ui-store';
import { useAppStore } from '../../store/app-store';
import { buildParentMap, treeDistance, PROJECT_ROOT, listHiddenDocIds } from '../../utils/document-sort';
import type { ChatSession } from '../../types';

/**
 * Returns the sessions LISTED from the open document, ordered for the chat
 * empty-list. Two modes:
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
 * ARCH (plan chats-unlisted-subtree): a CLIENT projection, not a server filter —
 * the list is fetched once per project and document switches do not re-fetch, so
 * a server filter keyed on the open document would go stale on the first
 * navigation. A session whose host_document_id lies in a `chats_unlisted`
 * subtree, viewed from OUTSIDE it, leaves the list (doc-less sessions are never
 * hidden; they have no subtree). The ACTIVE session is exempt ("leave it until
 * switched"): it stays listed until the user switches away — a reload restoring
 * a hidden chat as active is then exempt the same way.
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
  const activeSessionId = useChatStore(s => s.activeSessionId);
  const currentDocId = useAppStore(s => s.currentDocument?.document_id) ?? null;
  const documents = useAppStore(s => s.documents);
  const sortByProximity = useUIStore(s => s.getChatSortByProximity(currentDocId));

  return useMemo(() => {
    // chats_unlisted projection — hidden subtree hosts drop out, the ACTIVE
    // session stays, doc-less sessions (host null/absent) are never hidden.
    const hiddenDocIds = listHiddenDocIds(documents, currentDocId);
    const visible = hiddenDocIds.size === 0
      ? sessions
      : sessions.filter(s => s.session_id === activeSessionId
        || !s.host_document_id
        || !hiddenDocIds.has(s.host_document_id));

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
      return [...visible].sort(byProximity);
    }

    return [...visible].sort(byRecencyDesc);
  }, [sessions, activeSessionId, sortByProximity, currentDocId, documents]);
}

/**
 * Title-only, case-insensitive substring filter over chat sessions. A blank query
 * matches nothing; the minimum length is the caller's gate.
 */
// WHY fold both sides to a diacritic-free lowercase form: a typed "Ё" can arrive
// decomposed (Е + U+0308) while the stored title holds the precomposed U+0401,
// and a lowercase-only compare missed "Ёжи" in "ёжика". NFD + stripping combining
// marks also folds ё→е, so "ежик" finds "ёжик" either way. Latin Ë/ë (U+00CB/
// U+00EB — a Mac Option+U keystroke) is a visual twin of ё and is mapped to it
// first; other Latin look-alikes are a wrong-layout typo, not folded.
const fold = (v: string): string =>
  v.replace(/[\u00cb\u00eb]/g, 'ё').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();

export function filterSessionsByTitle(sessions: ChatSession[], query: string): ChatSession[] {
  const q = fold(query.trim());
  if (!q) return [];
  return sessions.filter(s => fold(s.title).includes(q));
}

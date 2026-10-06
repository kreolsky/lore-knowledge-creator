/** Pure tree helpers for the chat message tree and the activePath selector. */
import type { ChatMessage } from '../../types';
import type { ChatState } from './types';
import { registerChatResetHandler } from './reset-registry';

/**
 * Sentinel key for root-level messages (those with `parent_id === null`) in the
 * children map + `selectedSiblings` index. Centralized so a typo in any one site
 * cannot silently orphan root messages (a mismatched key would miss its bucket
 * and the message would never resolve onto the active path).
 */
export const ROOT_KEY = '__root__';

/**
 * Sentinel VALUE inside `selectedSiblings` (a map otherwise keyed by parent id
 * → chosen message id): marks the level whose active path is CUT — the
 * rewind-to-message feature. Keyed by the hidden message's parent (ROOT_KEY
 * for a first message); `resolveActivePath` stops there, so the hidden message
 * and everything below it drop off the rendered path and off sendMessage's
 * parent derivation. The branch itself is NOT deleted — once the next message
 * exists as a sibling, the fork switcher offers both. In-memory by design:
 * every reset site clears `selectedSiblings` wholesale and
 * `insertOptimisticUser` overwrites the entry with the temp id on send, so
 * session switch, reload and send end the rewind with zero extra code.
 */
export const REWIND_KEY = '__rewind__';

/** True when `path` ends at a REWIND_KEY cut — the rewind is what the user sees,
 *  not merely a sentinel parked somewhere in the map (an off-path one is inert). */
export function isPathRewound(path: ChatMessage[], selectedSiblings: Record<string, string>): boolean {
  const endKey = path.length > 0 ? path[path.length - 1].message_id : ROOT_KEY;
  return selectedSiblings[endKey] === REWIND_KEY;
}

/** Copy of `selectedSiblings` with every REWIND_KEY entry dropped. */
export function withoutRewind(selectedSiblings: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(selectedSiblings).filter(([, v]) => v !== REWIND_KEY));
}

// ─── Children map cache ──────────────────────────────────────────────────────

/** Build children map: parentId -> message[]. Cached by reference to avoid O(n) on each call. */
let _cachedMessages: ChatMessage[] | null = null;
let _cachedChildrenMap: Record<string, ChatMessage[]> = {};

export function buildChildrenMap(messages: ChatMessage[]): Record<string, ChatMessage[]> {
  if (messages === _cachedMessages) return _cachedChildrenMap;
  const map: Record<string, ChatMessage[]> = {};
  for (const m of messages) {
    const key = m.parent_id ?? ROOT_KEY;
    (map[key] ??= []).push(m);
  }
  _cachedMessages = messages;
  _cachedChildrenMap = map;
  return map;
}

/**
 * Parse a message's created_at to epoch ms. Missing/unparseable → -Infinity so
 * it can never falsely win a default-branch pick (consistent intent with the
 * backend's `... or ""` guard when it walks message chains).
 */
function nodeTimestampMs(m: ChatMessage): number {
  const t = Date.parse(m.created_at ?? '');
  return Number.isNaN(t) ? -Infinity : t;
}

/**
 * Compute `subtreeMax[messageId]` = the greatest created_at (ms) over that node
 * and all its descendants. Pure post-order DFS over the cached childrenMap,
 * O(n) for the whole forest, memoized per-id within the call. Chat trees are
 * small (sessions are paginated at 200 messages), so this needs no module-level
 * cache — `selectActivePath`'s reference-equality memo already bounds recompute.
 */
function computeSubtreeMax(
  childrenMap: Record<string, ChatMessage[]>,
): Record<string, number> {
  const maxByNode: Record<string, number> = {};
  const dfs = (m: ChatMessage): number => {
    const existing = maxByNode[m.message_id];
    if (existing !== undefined) return existing;
    let max = nodeTimestampMs(m);
    for (const child of childrenMap[m.message_id] ?? []) {
      const cm = dfs(child);
      if (cm > max) max = cm;
    }
    maxByNode[m.message_id] = max;
    return max;
  };
  for (const root of childrenMap[ROOT_KEY] ?? []) dfs(root);
  return maxByNode;
}

/**
 * Default-branch pick among fork-point siblings: the sibling whose subtree holds
 * the max created_at. Ties (identical max, e.g. equal timestamps or all-missing)
 * fall back to the last sibling in array order — the historical default — so the
 * pick stays deterministic.
 */
function pickFreshestSubtree(
  siblings: ChatMessage[],
  subtreeMax: Record<string, number>,
): ChatMessage {
  let best = siblings[0];
  let bestMax = subtreeMax[best.message_id] ?? -Infinity;
  // `>=` walks forward, so on a tie the LAST sibling in array order wins.
  for (let i = 1; i < siblings.length; i++) {
    const mx = subtreeMax[siblings[i].message_id] ?? -Infinity;
    if (mx >= bestMax) {
      best = siblings[i];
      bestMax = mx;
    }
  }
  return best;
}

/**
 * Walk the tree from roots, following selectedSiblings, to produce the active path.
 *
 * ARCH: the DEFAULT branch (no explicit `selectedSiblings[parentKey]`) is the
 * sibling whose subtree contains the message with the maximum created_at — NOT
 * "last sibling in array order". Why: messages arrive ORDER BY created_at ASC,
 * so "last sibling" is merely the latest FORK ROOT, which can be an old/short
 * branch while the freshest message lives in an earlier-forked subtree. Picking
 * the freshest subtree makes the shown branch the one the user most recently
 * extended, on both render (MessageList) and send (messages-slice parentId).
 * Explicit `selectedSiblings` still wins; during streaming it is always set
 * (streaming.ts), so this default never competes with an in-flight turn.
 */
export function resolveActivePath(
  messages: ChatMessage[],
  selectedSiblings: Record<string, string>,
): ChatMessage[] {
  const childrenMap = buildChildrenMap(messages);
  const subtreeMax = computeSubtreeMax(childrenMap);
  const path: ChatMessage[] = [];
  let currentChildren = childrenMap[ROOT_KEY] ?? [];

  while (currentChildren.length > 0) {
    // Pick explicit selection, else the freshest-subtree sibling.
    const parentKey = currentChildren[0].parent_id ?? ROOT_KEY;
    const selectedId = selectedSiblings[parentKey];
    // INVARIANT: a REWIND_KEY selection ends the active path at that level —
    // no sibling is chosen and nothing below is pushed.
    // Why: the next send must parent on the node BEFORE the hidden message so
    // it forks a sibling of the hidden branch, and sendMessage derives its
    // parent from this same path (activePath's last node).
    if (selectedId === REWIND_KEY) break;
    const chosen = currentChildren.find(m => m.message_id === selectedId)
      ?? pickFreshestSubtree(currentChildren, subtreeMax);
    path.push(chosen);
    currentChildren = childrenMap[chosen.message_id] ?? [];
  }
  return path;
}

/**
 * Resolve the ancestor chain of a message (leaf included), root-first.
 *
 * ARCH: the single shared ancestor-walk used by forkAndResend and regenerate —
 * one implementation, not a `while (cursor)` backwards-walk per call site.
 * Unlike `resolveActivePath` (which follows the user's selectedSiblings from the
 * roots), this walks a SPECIFIC message's parents regardless of the active
 * selection — fork/regenerate branch off an explicit node, not the active path.
 * Returns [] when `leafId` is unknown.
 */
export function resolveAncestorChain(messages: ChatMessage[], leafId: string): ChatMessage[] {
  const byId = new Map(messages.map(m => [m.message_id, m]));
  const chain: ChatMessage[] = [];
  let cursor: string | null = leafId;
  while (cursor) {
    const m = byId.get(cursor);
    if (!m) break;
    chain.unshift(m);
    cursor = m.parent_id;
  }
  // If the leaf itself was not found, byId.get(leafId) was undefined → chain empty.
  return chain.length > 0 && chain[chain.length - 1].message_id === leafId ? chain : [];
}

// ─── Derived selector ───────────────────────────────────────────────────────
// ARCH: activePath is derived state, never stored. Memoized by reference
// equality on messages + selectedSiblings — returns the same array when inputs
// haven't changed.  Streaming substitution is NOT done here; it belongs in the
// component render (MessageList via useMemo) because useSyncExternalStore
// requires getSnapshot to return a stable reference for the same store state.

let _memoMessages: ChatMessage[] | null = null;
let _memoSiblings: Record<string, string> | null = null;
let _memoBasePath: ChatMessage[] = [];

export function selectActivePath(
  s: Pick<ChatState, 'messages' | 'selectedSiblings'>,
): ChatMessage[] {
  if (s.messages !== _memoMessages || s.selectedSiblings !== _memoSiblings) {
    _memoMessages = s.messages;
    _memoSiblings = s.selectedSiblings;
    _memoBasePath = resolveActivePath(s.messages, s.selectedSiblings);
  }
  return _memoBasePath;
}

/** Reset memoization caches — called from store reset(). */
export function resetTreeCache(): void {
  _cachedMessages = null;
  _cachedChildrenMap = {};
  _memoMessages = null;
  _memoSiblings = null;
  _memoBasePath = [];
}

// self-register the cache clear on the chat-reset registry. misc-slice.reset
// fires it via clearChatCaches(); keeping the registration HERE means this module
// owns its own invalidation — a future cache field only needs a local clear + this
// line, not an edit to misc-slice.
registerChatResetHandler(resetTreeCache);

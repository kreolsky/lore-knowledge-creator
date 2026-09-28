/** Race-safe upsert-by-key — shared primitive for the chat stores.

# ARCH (Block 2.4 unification foothold): the actor's own mutation AND the realtime
#   frame it also receives can land in either order; both must converge to a single
#   entry (replace in place if the key matches, else append). Pure — no store
#   coupling — so note-chat-store and (future) chat-store slices share ONE definition
#   instead of each inlining its own dedup. The note-list PREPEND-on-new variant
#   (newest-first) stays in note-chat-store (`_upsertSession`) since chat-store uses a
#   different insert-and-pin session model — see `session-helpers.ts`.
 */
export function upsertByKey<T>(arr: T[], item: T, key: keyof T): T[] {
  const idx = arr.findIndex(a => a[key] === item[key]);
  if (idx === -1) return [...arr, item];
  const next = [...arr];
  next[idx] = item;
  return next;
}

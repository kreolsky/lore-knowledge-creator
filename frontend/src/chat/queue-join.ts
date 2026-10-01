/** Plan "chat-message-queue": pure coalescing of queued follow-up chips.

 * A burst of afterthoughts about the text the agent is writing *right now* belongs
 * to ONE follow-up turn, not N sequential ones. `joinQueued` folds the chips into a
 * single user message: parts trimmed, blank/whitespace-only chips dropped, joined
 * with a blank line between them — no bullets (the user wrote prose; bullets would
 * restate it as a list). Mirrors send-gate.ts in shape (pure, unit-tested). */

export function joinQueued(parts: string[]): string {
  return parts
    .map(p => p.trim())
    .filter(p => p.length > 0)
    .join('\n\n');
}

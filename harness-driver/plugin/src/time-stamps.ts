/**
 * The turn's time ground as its own context message — dsh's context-message
 * pattern (a `user/message` with a plugin-declared source kind, appended by an
 * `agent/pre-step` listener), never a decoration of the user's text.
 *
 * # ARCH: the stamps must not ride the user's own message. dsh's titler reads
 * exactly the `source.kind === 'user'` messages (session-title joins their
 * text blocks; its fallback takes the first words), so a stamp inside a short
 * prompt becomes part of the session title. Under Lore's own `lore-time` kind the titler never sees the bytes
 * while the model still receives them in the same step, right after the
 * user's message. The stamp BYTES are backend-owned (timezone, root anchor,
 * root-fork byte-stability — `_turn_time_stamps` in completions_turn.py);
 * this module only carries what arrived on the payload.
 */

import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type { ContextFormed } from '@deepseek-ai/dsh-llm'
import type { UserMessage } from '@deepseek-ai/dsh-session'

declare module '@deepseek-ai/dsh-llm' {
  interface MessageSourceMap {
    'lore-time': { kind: 'lore-time' } & ContextFormed
  }
}

/** The payload's `time_stamps` (array of stamp lines). Anything but an array
 * of strings degrades to [] / drops the non-string items — a malformed field
 * is no reason to fail a turn over a convenience. */
export function parseTimeStamps(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.filter((s): s is string => typeof s === 'string')
}

/** The stamps as ONE context user-message — a single text block of the lines
 * joined with `\n\n` — or undefined when there is nothing to
 * emit: no stamps, or a step that is not the turn's first (the ground names
 * the send time and must not repeat on later steps of a tool loop). */
export function timeStampMessage(stamps: readonly string[], step: number): UserMessage | undefined {
  if (step !== 1 || stamps.length === 0) return undefined
  return createUserMessage({
    content: [{ type: 'text', text: stamps.join('\n\n') }],
    source: { kind: 'lore-time' },
  })
}

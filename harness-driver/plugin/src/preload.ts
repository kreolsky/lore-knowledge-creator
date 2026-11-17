/**
 * The persona's preloaded skills and its chat-memory block as context
 * messages — the `lore-time` pattern (time-stamps.ts): a `user/message` with a
 * plugin-declared source kind, appended by an `agent/pre-step` listener.
 *
 * # ARCH: preload = activation + ONE injected message, never a system-prompt
 * edit. The backend sends `preload_skills` (names from the persona's
 * frontmatter) every turn; buildTurnPrep (turn.ts) adds every name that
 * resolves in the turn's catalog to `activated` (its pack tools callable) and
 * keeps the resolved skills here. The bodies enter rendered exactly as dsh
 * renders a loaded skill (`renderSkillContent`, the `skill` tool's own
 * output), under Lore's `lore-preload` kind; the backend-built memory block
 * enters under `lore-memory`.
 *
 * # INVARIANT: a preloaded skill is injected only when no `lore-preload`
 * message on the ACTIVE surface names it (`source.names`). The memory block
 * is at most ONE `lore-memory` node on the surface, tagged by the persona it
 * was built for; it is never recomputed mid-chat EXCEPT on a persona change,
 * where that node is REPLACED (dsh `surfaceOp: replace`) by the new persona's
 * block or by the no-memory note — one cache miss of the history after it,
 * accepted: a switch is rare and stale memory is a wrong answer. An untagged
 * node (written before the tag) is never swapped. Why: a pre-step
 * message is committed to the dsh log (model-visible ⟺ logged), so it
 * survives later turns; re-appending it every turn (as `lore-time` does)
 * would stack copies — yet a persona switched mid-chat brings skills the
 * earlier message never carried, so the preload dedupe is per name, not per
 * kind. A branch inherits its parent's copy (the fork seed copies the log);
 * after a compaction the old copy leaves the surface and the next turn
 * injects it again. Outside a persona switch the memory block is never
 * recomputed: a changed block would void the provider's cache of the whole
 * history.
 */

import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type { ContextFormed, Message } from '@deepseek-ai/dsh-llm'
import type { UserMessage } from '@deepseek-ai/dsh-session'
import { renderSkillContent } from '@deepseek-ai/dsh-skill'

import { frameSkillBody, LORE_SKILL_PROVIDER, type ResolvedSkill } from './skills.ts'

declare module '@deepseek-ai/dsh-llm' {
  interface MessageSourceMap {
    'lore-preload': { kind: 'lore-preload'; names: string[] } & ContextFormed
    'lore-memory': { kind: 'lore-memory'; persona?: string } & ContextFormed
  }
}

export const PRELOAD_KIND = 'lore-preload'
export const MEMORY_KIND = 'lore-memory'
export const MEMORY_OFF_NOTE = 'No conversation memory from past chats for this persona.'

/** The payload's `preload_skills`: non-empty strings, deduplicated in order.
 * Anything else degrades to [] — a malformed field is no reason to fail a turn. */
export function parsePreloadNames(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  const names = value.filter((s): s is string => typeof s === 'string' && s !== '')
  return [...new Set(names)]
}

/** The payload's `memory_block` ('' = none). */
export function parseMemoryBlock(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/** The payload's `memory_persona`: the persona id the block belongs to ('' = Default). */
export function parseMemoryPersona(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/** The preload names that resolve in the turn's catalog, as skills; every
 * unresolved name is logged and skipped (an unknown, disabled or unserved
 * skill never fails the turn). */
export function resolvePreloads(names: readonly string[], skills: readonly ResolvedSkill[]): ResolvedSkill[] {
  const byName = new Map(skills.map((s) => [s.name, s]))
  const resolved: ResolvedSkill[] = []
  for (const name of names) {
    const skill = byName.get(name)
    if (skill) {
      resolved.push(skill)
    } else {
      console.error(`[lore-skills] preload unknown ${name}`)
    }
  }
  return resolved
}

/** The skill names the `lore-preload` messages of the derived history carry. */
export function surfacePreloadedNames(history: readonly Message[]): Set<string> {
  const names = new Set<string>()
  for (const m of history) {
    const source = m.role === 'user' ? (m as UserMessage).source as { kind?: string; names?: unknown } : undefined
    if (source?.kind !== PRELOAD_KIND || !Array.isArray(source.names)) continue
    for (const name of source.names) if (typeof name === 'string') names.add(name)
  }
  return names
}

/** The context messages this step appends: on a turn's FIRST step only, the
 * preload message (the preloads no surface preload message names yet) — the
 * INVARIANT above. The caller logs what it actually admitted (`logInjected`). */
export function preloadMessages(
  preloads: readonly ResolvedSkill[], step: number,
  readHistory: () => readonly Message[],
): UserMessage[] {
  if (step !== 1 || !preloads.length) return []
  const history = readHistory()
  const out: UserMessage[] = []
  const onSurface = surfacePreloadedNames(history)
  const missing = preloads.filter((skill) => !onSurface.has(skill.name))
  if (missing.length) {
    const blocks = missing
      .map((skill) => ({ skill, content: frameSkillBody(skill) }))
      .filter(({ content }) => content !== '')
    if (blocks.length) {
      out.push(createUserMessage({
        content: [{
          type: 'text',
          text: blocks.map(({ skill, content }) => renderSkillContent({
            name: skill.name, provider: LORE_SKILL_PROVIDER, content,
          })).join('\n\n'),
        }],
        source: { kind: PRELOAD_KIND, names: blocks.map(({ skill }) => skill.name) },
      }))
    }
  }
  return out
}

/** A `lore-memory` node on the active surface: its seq and persona tag
 * (undefined = untagged, written before the tag). */
export interface MemoryNode { seq: number; persona: string | undefined }

/** What the memory block does on this step: append, replace one surface
 * node, or nothing — the INVARIANT above. */
export type MemoryAction =
  | { kind: 'append'; message: UserMessage }
  | { kind: 'replace'; seq: number; from: string; message: UserMessage }

/** The memory action of a turn's FIRST step. `readNodes` is read at the call,
 * right before the caller appends (a same-turn compaction may have removed
 * the node already ⇒ plain append path). */
export function memoryAction(
  memoryBlock: string, persona: string, step: number,
  readNodes: () => readonly MemoryNode[],
): MemoryAction | null {
  if (step !== 1) return null
  const node = readNodes()[0]
  const message = (text: string) => createUserMessage({
    content: [{ type: 'text', text }],
    source: { kind: MEMORY_KIND, persona },
  })
  if (!node) return memoryBlock ? { kind: 'append', message: message(memoryBlock) } : null
  if (node.persona === undefined || node.persona === persona) return null
  return { kind: 'replace', seq: node.seq, from: node.persona, message: message(memoryBlock || MEMORY_OFF_NOTE) }
}

/** The `lore-memory` nodes of a dsh session's active surface, in order. */
export function surfaceMemoryNodes(session: {
  surface: { nodes: readonly number[] }
  eventAt(seq: number): { type: string; data: unknown } | undefined
}): MemoryNode[] {
  const out: MemoryNode[] = []
  for (const seq of session.surface.nodes) {
    const ev = session.eventAt(seq)
    if (ev?.type !== 'user/message') continue
    const source = (ev.data as UserMessage).source as { kind?: string; persona?: unknown } | undefined
    if (source?.kind !== MEMORY_KIND) continue
    out.push({ seq, persona: typeof source.persona === 'string' ? source.persona : undefined })
  }
  return out
}

/** The driver-log line of a persona swap. */
export function logSwapped(action: { from: string; message: UserMessage }): void {
  const chars = action.message.content.reduce((n, b) => n + (b.type === 'text' ? b.text.length : 0), 0)
  const to = (action.message.source as { persona?: string }).persona ?? ''
  console.error(`[lore-memory] swapped persona=${action.from}→${to} chars=${chars}`)
}

/** The driver-log line per admitted message (`[lore-memory] injected chars=N`,
 * `[lore-skills] preloaded <names>`). */
export function logInjected(messages: readonly UserMessage[]): void {
  for (const m of messages) {
    const source = m.source as { kind: string; names?: string[] }
    if (source.kind === MEMORY_KIND) {
      const chars = m.content.reduce((n, b) => n + (b.type === 'text' ? b.text.length : 0), 0)
      console.error(`[lore-memory] injected chars=${chars}`)
    } else if (source.kind === PRELOAD_KIND) {
      console.error(`[lore-skills] preloaded ${(source.names ?? []).join(',')}`)
    }
  }
}

/**
 * The persona's preloaded skills, its chat-memory block and its auto-recall
 * block as context messages — the `lore-time` pattern (time-stamps.ts): a
 * `user/message` with a plugin-declared source kind, appended by an
 * `agent/pre-step` listener.
 *
 * # ARCH: preload = activation + ONE injected message, never a system-prompt
 * edit. The backend sends `preload_skills` (names or masks — the persona's
 * frontmatter `skills`, then the default-skills mask) every turn; buildTurnPrep
 * (turn.ts) adds every skill a mask resolves in the turn's VISIBLE catalog to
 * `activated` (its pack tools callable from the first step) and keeps the
 * resolved skills here. A hidden skill never preloads (the entry is logged as
 * skipped with its reason — resolvePreloads). The bodies enter rendered exactly as dsh
 * renders a loaded skill (`renderSkillContent`, the `skill` tool's own
 * output), under Lore's `lore-preload` kind; the backend-built memory block
 * enters under `lore-memory`, the auto-recall block under `lore-recall`.
 *
 * # INVARIANT: a preloaded skill is injected only when no `lore-preload`
 * message on the ACTIVE surface names it (`source.names`). The memory block
 * is at most ONE `lore-memory` node on the surface and the auto-recall block
 * at most ONE `lore-recall` node — each tagged by the persona it was built for
 * (memoryAction takes the kind), and neither is ever recomputed mid-chat
 * EXCEPT on a persona change, where that node is REPLACED (dsh `surfaceOp:
 * replace`) by the new persona's block or by the kind's own no-block note —
 * one cache miss of the history after it, accepted: a switch is rare and
 * stale memory is a wrong answer. A node holding that off note is NOT
 * "already this persona's": a later block for the same persona replaces it,
 * because the server records the block it sends as delivered. An untagged node
 * (written before the tag) is never swapped. Why: a pre-step
 * message is committed to the dsh log (model-visible ⟺ logged), so it
 * survives later turns; re-appending it every turn (as `lore-time` does)
 * would stack copies — yet a persona switched mid-chat brings skills the
 * earlier message never carried, so the preload dedupe is per name, not per
 * kind. A branch inherits its parent's copy (the fork seed copies the log);
 * after a compaction the old copy leaves the surface and the next turn
 * injects it again. Outside a persona switch neither block is ever
 * recomputed: a changed block would void the provider's cache of the whole
 * history.
 */

import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type { ContextFormed, Message } from '@deepseek-ai/dsh-llm'
import type { UserMessage } from '@deepseek-ai/dsh-session'
import { renderSkillContent } from '@deepseek-ai/dsh-skill'

import {
  frameSkillBody, LORE_SKILL_PROVIDER, skillEntryMatches,
  type HiddenSkill, type ResolvedSkill,
} from './skills.ts'

declare module '@deepseek-ai/dsh-llm' {
  interface MessageSourceMap {
    'lore-preload': { kind: 'lore-preload'; names: string[] } & ContextFormed
    'lore-memory': { kind: 'lore-memory'; persona?: string; off?: boolean } & ContextFormed
    'lore-recall': { kind: 'lore-recall'; persona?: string; off?: boolean } & ContextFormed
  }
}

export const PRELOAD_KIND = 'lore-preload'
export const MEMORY_KIND = 'lore-memory'
export const RECALL_KIND = 'lore-recall'
/** The two kinds of a persona's backend-built block, each at most one node per surface. */
export type MemoryKind = typeof MEMORY_KIND | typeof RECALL_KIND
export const MEMORY_OFF_NOTE = 'No conversation memory from past chats for this persona.'
export const RECALL_OFF_NOTE = 'No excerpts of earlier conversations are recalled for this persona.'

/** The payload's `preload_skills`: non-empty strings, deduplicated in order —
 * each an exact skill name or a prefix mask (`main-*`, skills.ts
 * skillEntryMatches). Anything else degrades to [] — a malformed field is no
 * reason to fail a turn. The backend appends the default-skills mask unless
 * the persona says `preload_defaults: false` (routes/chat/
 * completions_harness._preload_default_skills); it expands only here. */
export function parsePreloadNames(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  const names = value.filter((s): s is string => typeof s === 'string' && s !== '')
  return [...new Set(names)]
}

/** The payload's `memory_block` / `recall_block` ('' = none). */
export function parseBlock(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/** The payload's `memory_persona`: the persona id the blocks belong to ('' = Default). */
export function parseMemoryPersona(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

/** The preload entries resolved against the persona's VISIBLE catalog, as
 * skills in catalog order; an entry is a name or a mask (skills.ts
 * skillEntryMatches), and a skill two entries name is kept once.
 *
 * An entry that resolves to nothing visible is logged and skipped (an
 * unknown, disabled or unserved skill never fails the turn). When the entry
 * names skills the persona HIDES (`hidden`, skills.ts splitVisibleSkills), the
 * line says so with the reason — `deny <entry>` or `outside allow` — instead of
 * the bare `unknown`: the persona's author wrote a preload its own allow/deny
 * cancels, and the log is the only place that says it. */
export function resolvePreloads(
  names: readonly string[], skills: readonly ResolvedSkill[],
  hidden: readonly HiddenSkill[] = [],
): ResolvedSkill[] {
  const resolved = new Map<string, ResolvedSkill>()
  for (const entry of names) {
    const matches = skills.filter((skill) => skillEntryMatches(skill.name, entry))
    for (const skill of matches) resolved.set(skill.name, skill)
    if (matches.length) continue
    const gone = hidden.filter(({ skill }) => skillEntryMatches(skill.name, entry))
    console.error(gone.length
      ? `[lore-skills] preload skipped ${entry}: ${
        gone.map(({ skill, reason }) => `${skill.name} hidden (${reason})`).join('; ')}`
      : `[lore-skills] preload unknown ${entry}`)
  }
  return [...resolved.values()]
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

/** A `lore-memory` / `lore-recall` node on the active surface: its seq, its
 * persona tag (undefined = untagged, written before the tag) and whether it
 * holds the kind's off note. */
export interface MemoryNode { seq: number; persona: string | undefined; off: boolean }

/** What a persona's block of one kind does on this step: append, replace one
 * surface node, or nothing — the INVARIANT above. */
export type MemoryAction =
  | { kind: 'append'; message: UserMessage }
  | { kind: 'replace'; seq: number; from: string; message: UserMessage }

/** The action of a turn's FIRST step for the persona's block of `kind`:
 * `block` ('' = none this turn), `persona` = the persona it belongs to,
 * `offNote` = what a persona switch says when the new persona brings none.
 * `readNodes` is read at the call, right before the caller appends (a
 * same-turn compaction may have removed the node already ⇒ plain append path). */
export function memoryAction(
  block: string, persona: string, step: number,
  readNodes: () => readonly MemoryNode[],
  kind: MemoryKind, offNote: string,
): MemoryAction | null {
  if (step !== 1) return null
  const node = readNodes()[0]
  // WHY the kind is spelled out per branch: the source type is a discriminated
  // union, and a `kind` of the union type does not narrow to one member.
  const message = (text: string, off = false) => {
    const tag = { persona, ...(off ? { off: true } : {}) }
    return createUserMessage({
      content: [{ type: 'text', text }],
      source: kind === MEMORY_KIND ? { kind: MEMORY_KIND, ...tag } : { kind: RECALL_KIND, ...tag },
    })
  }
  if (!node) return block ? { kind: 'append', message: message(block) } : null
  if (node.persona === undefined) return null
  // An off note is not "already this persona's": the server sent no block then
  // and records the one it sends now as delivered — it must reach the surface.
  if (node.persona === persona && !(node.off && block)) return null
  return { kind: 'replace', seq: node.seq, from: node.persona, message: message(block || offNote, block === '') }
}

/** The nodes of `kind` (`lore-memory` | `lore-recall`) on a dsh session's
 * active surface, in order. */
export function surfaceMemoryNodes(
  session: {
    surface: { nodes: readonly number[] }
    eventAt(seq: number): { type: string; data: unknown } | undefined
  },
  kind: MemoryKind,
): MemoryNode[] {
  const out: MemoryNode[] = []
  for (const seq of session.surface.nodes) {
    const ev = session.eventAt(seq)
    if (ev?.type !== 'user/message') continue
    const source = (ev.data as UserMessage).source as { kind?: string; persona?: unknown; off?: unknown } | undefined
    if (source?.kind !== kind) continue
    out.push({ seq, persona: typeof source.persona === 'string' ? source.persona : undefined, off: source.off === true })
  }
  return out
}

/** The driver-log line of a swap (`[lore-memory]` / `[lore-recall]`). */
export function logSwapped(action: { from: string; message: UserMessage }): void {
  const chars = action.message.content.reduce((n, b) => n + (b.type === 'text' ? b.text.length : 0), 0)
  const source = action.message.source as { kind?: string; persona?: string }
  const to = source.persona ?? ''
  console.error(`[${source.kind ?? MEMORY_KIND}] swapped persona=${action.from}→${to} chars=${chars}`)
}

/** The driver-log line per admitted message (`[lore-memory] injected chars=N`,
 * `[lore-recall] injected chars=N`, `[lore-skills] preloaded <names>`). */
export function logInjected(messages: readonly UserMessage[]): void {
  for (const m of messages) {
    const source = m.source as { kind: string; names?: string[] }
    if (source.kind === MEMORY_KIND || source.kind === RECALL_KIND) {
      const chars = m.content.reduce((n, b) => n + (b.type === 'text' ? b.text.length : 0), 0)
      console.error(`[${source.kind}] injected chars=${chars}`)
    } else if (source.kind === PRELOAD_KIND) {
      console.error(`[lore-skills] preloaded ${(source.names ?? []).join(',')}`)
    }
  }
}

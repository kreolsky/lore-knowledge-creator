/**
 * Headless dsh conversation assembler — the engine Lore renders over.
 *
 * # SYSTEM: dsh-conversation — dsh assembles the dialogue, Lore only renders
 *   it. `ConversationNodeAssembler` plus the eleven conversation-node
 *   definitions dsh registers run here on a BARE cordis context: no slots, no
 *   locale, no connection, no React. The output is plain data
 *   (`ChatConversationViewNode[]`), so the browser reads node data and never
 *   recomputes it.
 *
 * # ARCH: ONE engine for both paths. `replaceWindow` is the reload, `append`
 *   is the live tail, and they are the same assembler — the reload==live
 *   parity Lore kept rebuilding by hand is this constructor's contract, not a
 *   pair of synchronized code paths.
 *
 * # INVARIANT: an event whose seq is BELOW the fed tail enters through
 *   `splice`, never `append`. Why: `append` is dsh's contiguous-tail path and
 *   its location index takes each appended event as the new cursor
 *   (`appendNonBoundary` in conversation-location-index.ts sets currentTurn /
 *   currentStep from the payload), so a late out-of-band mint — the detached
 *   image outcome — rewinds the cursor and re-locates every following
 *   turn-less dsh event. Measured: a user message after one such append moved
 *   from session level into the previous turn, and the reload of the identical
 *   event set disagreed with the streamed one. `prepend` rebuilds the index
 *   over the sorted inputs and keeps node identities, so it restores the
 *   parity exactly.
 *
 * # INVARIANT: dsh's definitions register verbatim and Lore's own register
 *   through `registerLoreNodes` — the two never mix: no Lore kind is added to
 *   what dsh registers (that file is vendored upstream), and no dsh kind is
 *   re-defined here. Why: a Lore-minted node is a fact dsh cannot state, and
 *   the SAME registry is what keeps one engine, one node stream — never a
 *   second pipeline.
 *
 * Bundled by tsdown into one ESM file inside the harness image (which already
 * builds the whole dsh workspace) because the peer graph is ~16 workspace
 * packages and the runtime pins a zustand major below Lore's — the bundle
 * seals those deps and keeps both sides on the pinned sha.
 */

import { Context } from '@deepseek-ai/cordis'
import type { ConversationPublication } from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { ChatConversationViewNode, ChatSnapshot } from '@deepseek-ai/dsh-client-ui-chat/client'
import type { SessionEventLikeEntry } from '@deepseek-ai/dsh-api-session-controller/client'
// WHY: relative into the workspace. The packages' mapped specifiers reach their
// `client` barrels, which export the React apply layer (and its css modules);
// the assembler, the two registries and the node definitions are React-free,
// and these are the only paths to them. Type-only imports above are erased and
// may use the barrels. This directory is copied to /dsh/lore-conversation,
// beside packages/.
import { ConversationNodeAssembler } from '../../packages/client/ui-conversation/src/client/conversation/assembler.ts'
import { ConversationEventRegistry } from '../../packages/client/ui-conversation/src/client/conversation/event-registry.ts'
import { ConversationViewRegistry } from '../../packages/client/ui-conversation/src/client/conversation/view-registry.ts'
import { inspectRequestPrompt } from '../../packages/client/ui-conversation/src/client/contract/request-inspection.ts'
import { inspectSystemPrompt } from '../../packages/client/ui-conversation/src/client/contract/system-prompt.ts'
import { registerConversationNodes } from '../../packages/client/ui-chat/src/client/conversation-nodes/register.ts'
import { registerLoreNodes } from './lore-nodes.ts'

export type {
  ChatConversationViewNode,
  SessionEventLikeEntry,
  ConversationPublication,
}

/** The whole surface Lore consumes: feed events, read ordered nodes. */
export interface LoreConversation {
  /** Replace the loaded window (reload, resync, gap repair). */
  replaceWindow(entries: readonly SessionEventLikeEntry[], hasMore: boolean): ConversationPublication
  /** Append one live tail event — its seq must be above every fed event. */
  append(input: SessionEventLikeEntry): ConversationPublication
  /**
   * Feed one event whose position may be behind the tail (an out-of-band
   * Lore mint). Routes to `append` when it IS the tail, otherwise rebuilds
   * the window around it. Use this whenever the seq is not known to lead.
   */
  splice(input: SessionEventLikeEntry): ConversationPublication
  /** Materialize and read the chat target in render order. */
  nodes(): readonly ChatConversationViewNode[]
}

/**
 * Build one conversation engine with dsh's own node definitions installed,
 * plus Lore's four own cards (image-gen, verdict-ask, halt, compaction-mint).
 * @returns the feed/read surface; each instance owns its own context.
 */
export function createLoreConversation(): LoreConversation {
  const ctx = new Context()
  const events = new ConversationEventRegistry(ctx)
  const views = new ConversationViewRegistry(ctx)
  // WHY: the node definitions reach the assembler through `ctx.uiConversation`
  // — the service dsh's client installs over a session connection (assembly.ts
  // `UiConversation`). Headless, the service is the four things the
  // definitions read off it: the two registries and the two pure prompt
  // inspectors it forwards to, so no connection, store or React is pulled in.
  ctx.provide('uiConversation')
  ctx.set('uiConversation', { events, views, inspectSystemPrompt, inspectRequestPrompt })
  registerConversationNodes(ctx)
  registerLoreNodes(ctx)
  const assembler = new ConversationNodeAssembler(events, views)
  // The chat target materializes only once activated; activation is
  // monotonic, so once is enough for the instance's lifetime.
  assembler.activateTarget('chat')
  let tailSeq = Number.NEGATIVE_INFINITY
  let hasMoreHistory = false

  const feedTail = (input: SessionEventLikeEntry): ConversationPublication => {
    tailSeq = Math.max(tailSeq, input.event.seq)
    return assembler.append(input)
  }

  return {
    replaceWindow: (entries, hasMore) => {
      tailSeq = entries.reduce((high, entry) => Math.max(high, entry.event.seq), Number.NEGATIVE_INFINITY)
      hasMoreHistory = hasMore
      return assembler.replaceWindow(entries, hasMore)
    },
    append: feedTail,
    splice: input => input.event.seq > tailSeq
      ? feedTail(input)
      : assembler.prepend([input], hasMoreHistory),
    nodes: () => {
      assembler.flush()
      const snapshot = assembler.snapshot('chat') as ChatSnapshot | undefined
      if (snapshot === undefined) throw new Error('dsh chat conversation view is not registered')
      const ordered: ChatConversationViewNode[] = []
      for (const id of snapshot.order) {
        const node = snapshot.nodes.get(id)
        if (node !== undefined) ordered.push(node)
      }
      return ordered
    },
  }
}

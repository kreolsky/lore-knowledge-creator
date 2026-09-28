/**
 * Typed surface of the committed assembler bundle (lore-conversation.js, beside
 * this file). The bundle is BUILT inside the harness image from
 * `harness-driver/conversation/src/index.ts` and committed here (plan
 * lore-renders-dsh-conversation step 1); this hand-kept declaration pins the
 * ONE surface Lore consumes and keeps tsc out of the ~10k-line artifact. It is
 * a build-time coupling: the Vite build fails loudly when the .js is missing —
 * there is no stale-copy fallback.
 */

export interface SessionEventLike {
  readonly seq: number
  readonly type: string
  readonly data?: unknown
  readonly time?: number
  readonly ignorable?: boolean
  readonly surfaceOp?: unknown
  readonly sourceEventSeqs?: number[]
}

/** The assembler's input entry (dsh `SessionEventLikeEntry`): a committed log
 * event, or a transient one that never enters the log (a live stream chunk). */
export type SessionEventLikeEntry =
  | { readonly type: 'event'; readonly event: SessionEventLike }
  | { readonly type: 'transient'; readonly event: SessionEventLike }

export interface ChatConversationViewNode {
  readonly key: string
  readonly kind: string
  readonly id: string
  readonly target: 'chat'
  readonly anchorSeq: number
  readonly visibility: 'visible' | 'hidden'
  readonly data: unknown
}

export type ConversationPublication = 'none' | 'animation-frame' | 'immediate'

export interface LoreConversation {
  replaceWindow(
    entries: readonly SessionEventLikeEntry[],
    hasMore: boolean,
  ): ConversationPublication
  append(input: SessionEventLikeEntry): ConversationPublication
  splice(input: SessionEventLikeEntry): ConversationPublication
  nodes(): readonly ChatConversationViewNode[]
}

export declare function createLoreConversation(): LoreConversation

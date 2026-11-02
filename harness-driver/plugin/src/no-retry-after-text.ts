/**
 * Stops dsh's retry of a model request that failed after printing reply text.
 *
 * dsh asks the `agent/request-error` waterfall whether to retry a failed
 * stream, and its llm-retry plugin re-sends the whole request — a fresh
 * generation that discards what was printed. This listener runs first and
 * vetoes the chain when the attempt that just failed carried reply text in a
 * Lore chat turn; any other failure — and every subagent's — passes through to
 * dsh's own recovery (retry, compaction, image offload) unchanged.
 */

import type { Context } from '@deepseek-ai/cordis'
import { joinAssistantStreamText } from '@deepseek-ai/dsh-llm'
import type { TurnToolCtx } from './tools.ts'

/** Whether the session's last event is the failed attempt of this step and it printed reply text. */
function attemptPrintedText(session: any, turn: number, step: number): boolean {
  if (typeof session?.eventAt !== 'function' || typeof session.seq !== 'number') return false
  // WHY the last event: dsh appends the failed attempt immediately before
  // dispatching the waterfall, so nothing sits between them.
  const event = session.eventAt(session.seq - 1)
  if (event?.type !== 'assistant/attempt') return false
  if (event.data?.turn !== turn || event.data?.step !== step) return false
  const stream = event.data?.stream
  return Array.isArray(stream) && joinAssistantStreamText(stream).trim() !== ''
}

/**
 * Register the veto ahead of every other `agent/request-error` listener.
 * @param ctx - the plugin context.
 * @param ctxLookup - the Lore turn context of a dsh session; none for a subagent's.
 */
export function registerNoRetryAfterText(
  ctx: Context, ctxLookup: (dshId: string) => TurnToolCtx | undefined,
): void {
  ctx.on('agent/request-error' as never, ((payload: any, next: () => Promise<unknown>) => {
    const session = payload?.agent?.session
    // WHY chat turns only: a subagent's text never reaches the chat, so its
    // retry rewrites nothing the user saw and keeps its recovery value.
    if (!ctxLookup(String(session?.id ?? ''))) return next()
    // INVARIANT: once the model has printed reply text, a stream failure ends
    // the turn — no retry from scratch; a failure before any text keeps dsh's retry.
    // Why: operator ruling — a retry rewrites the answer, so the printed text
    // and a different new variant pile up in the chat.
    if (attemptPrintedText(session, payload?.turn, payload?.step)) {
      return Promise.resolve(undefined)
    }
    return next()
  }) as never, true)
}

/**
 * The plugin's fold of each dsh session's live assistant stream — the reload
 * baseline `/session-entries` serves on the OPEN turn.
 *
 * # SYSTEM: harness-driver (reload baseline half) — the transient half of the
 *   relay is never replayed (ws-events.ts: a resync reads the settled log),
 *   so a reload mid-step used to lose the streamed text until settlement
 *   (chunks with no open attempt are dropped, conversation-feed.ts). dsh's
 *   own reconnect answer is the accumulator: this fold keeps one
 *   SessionAssistantStreamAccumulator per dsh session id, fed by the SAME
 *   sink that relays `dsh_stream` frames, and /session-entries serves
 *   snapshot() on the trailing open turn (entries.ts) — the backend passes it
 *   onto the open row, the browser seats the attempt and replays the compact
 *   records. A missed frame (a revision gap) RESETS dsh's fold: the snapshot
 *   then carries no active attempt, no baseline is served, and the reload
 *   degrades to today's behaviour — never a wrong one.
 */

import type { AssistantStreamFrame } from '@deepseek-ai/dsh-agent'
import { SessionSeq, type SessionSeqCursor } from '@deepseek-ai/dsh-session'
import type { SessionAssistantStreamBaseline } from '@deepseek-ai/dsh-api-session-controller/types'
// WHY relative into the workspace: SessionAssistantStreamAccumulator is not in
// the package's export map (its client barrel is the only shipped consumer);
// the plugin sits inside the workspace at /dsh/lore-driver, so the source
// path is the same seam the conversation bundle reaches unexported sources
// through (conversation/src/index.ts).
import { SessionAssistantStreamAccumulator } from '../../packages/api/session-controller/src/assistant-stream.ts'

/** The fold + its durable cursor input, keyed by dsh session id. */
export interface SessionStreamBaselines {
  /** Record the session's last observed event seq (the session/event tap's
   * own numbering) — the durable cursor a subsequent stream start records as
   * its `startedAfterSeq`. */
  observe(sessionId: string, seq: number | undefined): void
  /** Fold one `agent/assistant-stream` frame of that session — the same
   * frame relayAssistantStream pushes as `dsh_stream`. */
  accept(sessionId: string, frame: unknown): void
  /** The reconnect baseline, or undefined for a session that never streamed. */
  snapshot(sessionId: string): SessionAssistantStreamBaseline | undefined
  /** Drop one session's fold (the session-delete forward). */
  forget(sessionId: string): void
}

interface SessionFold {
  lastSeq: SessionSeqCursor
  acc: SessionAssistantStreamAccumulator
}

export function createSessionStreamBaselines(): SessionStreamBaselines {
  const folds = new Map<string, SessionFold>()

  const foldOf = (sessionId: string): SessionFold => {
    let fold = folds.get(sessionId)
    if (fold === undefined) {
      fold = { lastSeq: -1, acc: new SessionAssistantStreamAccumulator() }
      folds.set(sessionId, fold)
    }
    return fold
  }

  return {
    observe(sessionId, seq) {
      if (seq === undefined || !Number.isFinite(seq)) return
      const fold = foldOf(sessionId)
      if (seq > fold.lastSeq) fold.lastSeq = SessionSeq(seq)
    },
    accept(sessionId, frame) {
      const fold = foldOf(sessionId)
      fold.acc.accept(frame as AssistantStreamFrame, fold.lastSeq)
    },
    snapshot(sessionId) {
      return folds.get(sessionId)?.acc.snapshot()
    },
    forget(sessionId) {
      folds.delete(sessionId)
    },
  }
}

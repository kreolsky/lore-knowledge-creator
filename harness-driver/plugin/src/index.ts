/**
 * Lore driver plugin — the RENTED agent loop behind Lore's driver contract.
 *
 * # SYSTEM: harness-driver — the rented brain's driver: serves the HTTP
 *   endpoints the Lore backend speaks (`POST /followup` + `POST /stop` — the
 *   driver-owned turn, `POST /session-fork`, `POST /session-entries`,
 *   `GET/POST /approvals`, `POST /approvals/resolve`, `GET /capability`, `GET
 *   /health`) plus the standing event channel `/ws/events`
 *   (subscribe/unsubscribe by session id — ws-events.ts).
 *   Lore owns identity/RBAC/persistence
 *   and its tools — they arrive as payload
 *   proxies (tools.ts) with the skills pack split (skills.ts), per-turn system
 *   prompt handed over by Lore's own prep layer (registered as the agent's
 *   COMPLETE prompt section — no second copy grows here). The turn pipeline
 *   drives the dsh Agent per turn (resume-or-create → followup → quiescence →
 *   flush → dispose): stateless on our side between turns, the canonical
 *   conversation lives in the dsh session log (JSONL under $DSH_HOME/sessions
 *   — a HOST bind mount in compose).
 *
 * # ARCH: Lore session id → dsh session id. A chat's dsh session id EQUALS
 *   its Lore session id (a branch is its own
 *   session, seeded under its own id by /session-fork before its first
 *   turn); nothing is ever re-pointed. `lore-session-map.json` stays as a
 *   READ-ONLY legacy resolution: chats that forked under the old leaf-move
 *   scheme carry entries, and SessionMap.get defaults to identity for
 *   everything else. Entry ids served to Lore (`e<seq>`) are stable across
 *   forks: a fork's seed RETAINS the parent prefix with the same seqs.
 */

import http from 'node:http'
import type { Context } from '@deepseek-ai/cordis'

// INVARIANT: never import boot-env.ts here. Why: it is the process entry and
// awaits runCli() at top level, which loads this plugin — importing it back
// waits on its own unfinished evaluation and the harness never listens.
import { attachEventsChannel, createSessionEventTap, loadWs, relayAssistantStream } from './ws-events.ts'
import { createSessionStreamBaselines } from './stream-baselines.ts'
import { registerLoreApprovalBridge } from './approvals.ts'
import { disposeLoreTools, loreTurnCtxFor } from './tools.ts'
import { SessionMap } from './sessions.ts'
import { handleRequest, authorized, PORT } from './http.ts'

export const name = 'lore-driver'
export const inject = ['agents', 'approval', 'sessionPersistence', 'sessions', 'skills', 'systemPrompt', 'tools', 'subagents']

// The plugin's public surface, unchanged for every importer: the split files
// own the bodies; index.ts stays the cordis entry
// (name/inject/apply) and re-exports the symbols the tests import from here.
export {
  AGENT_KEY_REF, PROVIDER, ensureAgentKey, ensureModelEntry, ensureTitleConfig,
  ensureTitleEntry, ensureWebSearch,
} from './ensure.ts'
export {
  anthropicRoute, contextUsageFrame, followupTurns, loreRoute, prepareUserContent,
  promptContent, ROUTE_BY_API, turnConfigProblem,
} from './turn.ts'
export { forkModel, sessionEntries, sessionFork } from './sessions.ts'

export function apply(ctx: Context): void {
  const map = new SessionMap(process.env.DSH_HOME)
  // The ONE session-event tap: apply() holds the single
  // ctx.on('session/event') subscription; the standing WS channel and the
  // per-turn watch subscribe through the tap, so every relay sink sees the
  // same events in the same synchronous order.
  const tap = createSessionEventTap()
  const offTap = ctx.on('session/event', (session: any, ev: any) => tap.emit(session, ev))
  const server = http.createServer((req, res) => { void handleRequest(ctx, map, tap, channel, baselines, req, res) })
  // The reload-baseline fold's durable cursor: every session event observed
  // here advances the session's last-seen seq (watch phase — before the
  // channel's mapped frames), so a stream start records the seq it started
  // after (stream-baselines.ts observe).
  const baselines = createSessionStreamBaselines()
  const offObserve = tap.subscribe((session: any, ev: any) => {
    baselines.observe(
      String(session?.id ?? ''),
      typeof ev?.seq === 'number' ? ev.seq : undefined,
    )
  }, 'watch')
  // The approval bridge: dsh's approval/request waterfall is answered
  // by the parked question the verdict endpoints below resolve.
  registerLoreApprovalBridge(ctx, loreTurnCtxFor)
  // The standing event channel rides the SAME server and the SAME secret
  // gate; ws resolves lazily (see ws-events.ts's loadWs ARCH) — a missing ws
  // logs loudly, and the turn is refused (no delivery path) rather than run
  // silently into nothing.
  const ws = loadWs(process.env.DSH_HOME)
  const channel = ws
    ? attachEventsChannel({
      server, tap, resolve: (id) => map.get(id), authorized, ws,
    })
    : null
  // The assistant-stream tap: dsh publishes live chunks on
  // `agent/assistant-stream` (the v4 log holds only SETTLED events); the
  // sink relays each frame verbatim as `dsh_stream` through the same
  // channel and folds it into the session's reload baseline — registered
  // beside the session/event tap, `{ global: true }`
  // exactly as dsh's own session-controller registers it, so every agent's
  // publications reach the sink regardless of scope. No channel (no ws): no
  // turns run (followup refuses), so nothing is lost by not tapping.
  // WHY the cast: `Context` here is bare cordis; the `{ global }` option is
  // dsh-scope's augmentation of `on`, which this plugin's type graph never
  // imports (the session/event tap above types its handler `any` for the
  // same reason).
  const offStream = channel
    ? ctx.on('agent/assistant-stream', relayAssistantStream(channel, baselines), { global: true } as never)
    : null
  server.listen(PORT, () => {
    console.error(`[lore-driver] listening on ${PORT}`
      + (channel ? ' (+ /ws/events)' : ''))
  })
  ctx.on('dispose', () => {
    offTap()
    offObserve()
    offStream?.()
    channel?.close()
    server.close()
    disposeLoreTools()
  })
}

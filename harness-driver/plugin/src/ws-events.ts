/**
 * The standing event channel — `/ws/events` — plus the ONE session-event tap
 * every relay sink subscribes to.
 *
 * # SYSTEM: harness-driver (standing channel half) — the SUBSCRIPTION half of
 *   the one relay: the SAME frames map.ts mints (mapEvent verbatim, the lore
 *   mints included), delivered per subscribed
 *   dsh session id ENVELOPED as {type:'session_frame', session_id, frame}
 *   (the frames carry no session attribution of their own, and the
 *   backend's one socket subscribes to many sessions), PLUS the driver's
 *   turn-lifecycle frames (model_update, context_usage, error) pushed by the
 *   followup runner — addressed by session, unsequenced, never
 *   window-keyed — and the descendant heartbeat (lore/child-activity, see
 *   childActivityRoot), addressed to the ROOT session. Turn lifecycle
 *   frames are re-derivable from the turn's outcome and never replayed: a
 *   consumer that lost frames resyncs through POST /session-entries +
 *   since_seq (the extended projection), never through a second timeline
 *   replayed here. The TRANSIENT half rides the same channel: dsh's own live
 *   chunk publication (agent/assistant-stream — the log holds
 *   only settled events) relays as {type:'dsh_stream', frame} through
 *   relayAssistantStream below — same envelope, same auth, addressed by the
 *   emitting agent's session, never replayed (a resync reads the settled
 *   log; the browser's assembler supersedes the transient rows with the
 *   settlement).
 *
 * # ARCH: subscribe/unsubscribe by session id, resolved through the identity
 *   map AT SUBSCRIBE TIME (a lore id maps to its current dsh id; a dsh id
 *   passes through — SessionMap.get's own fallback; the map is READ-ONLY
 *   legacy resolution). Nothing is ever
 *   re-pointed: a session's dsh id is fixed for its lifetime (dsh id =
 *   Lore id), so a subscription's routing holds until unsubscribe.
 *
 * # ARCH: `ws` is not a dependency of the dsh workspace root — it ships in
 *   the DSH profile fallback ($DSH_HOME/profiles/node_modules, app-boot's
 *   flat module dir for out-of-tree plugin deps), which Node's parent walk
 *   reaches only from INSIDE the home, not from the plugin's own directory
 *   (verified empirically in the image: a bare import dies with
 *   ERR_MODULE_NOT_FOUND). loadWs resolves it through a require anchored in
 *   the profiles dir — dsh's own mechanism, pointed at explicitly — lazily
 *   at attach; a composition where it cannot resolve logs loudly and serves
 *   NO channel — and without the channel /followup refuses every turn with
 *   503 (the backend has no other delivery path).
 *
 * # ARCH: same trust boundary as the HTTP endpoints and no port of its own:
 *   the upgrade rides the existing HTTP server (compose network only),
 *   guarded by the same x-driver-secret header; a wrong path or secret is
 *   refused with 404/401 before any WS handshake.
 *
 * # INVARIANT: one TurnMapState per (socket, subscribed session), seeded at
 *   subscribe and carried across turns. Why: mapEvent's mint coordinates
 *   read state.turn, which each turn/start re-seeds — a state shared across
 *   SESSIONS would cross coordinate systems, and the replay projection
 *   (entries.ts) already holds this per-session discipline; the standing
 *   channel must not be a second divergent mapping.
 */

import { createRequire } from 'node:module'
import { join } from 'node:path'
import type http from 'node:http'

import { mapEvent, newTurnMapState, type DshEvent, type TurnMapState } from './map.ts'
import type { SessionStreamBaselines } from './stream-baselines.ts'

// ── The ONE session-event tap.
//
// apply() owns the single ctx.on('session/event') subscription and forwards
// into this tap; every relay sink — the standing WS channel — subscribes
// here. Fan-out is synchronous, in subscription order, so a turn's wire
// order cannot be reordered by the channel.

export type SessionEventSink = (session: unknown, ev: unknown) => void

export interface SessionEventTap {
  emit(session: unknown, ev: unknown): void
  /** `phase`: 'watch' sinks observe every event BEFORE 'relay' sinks (the
   * default). Why pinned in the contract and not left to registration luck:
   * the followup runner's turn watcher pushes turn-lifecycle frames
   * (context_usage) that must reach subscribers BEFORE the same event's
   * mapped frames — the wire order, by construction. */
  subscribe(sink: SessionEventSink, phase?: 'watch' | 'relay'): () => void
}

export function createSessionEventTap(): SessionEventTap {
  const watch: SessionEventSink[] = []
  const relay: SessionEventSink[] = []
  const register = (list: SessionEventSink[], sink: SessionEventSink) => {
    list.push(sink)
    return () => {
      const index = list.indexOf(sink)
      if (index >= 0) list.splice(index, 1)
    }
  }
  return {
    emit(session, ev) {
      for (const sink of [...watch, ...relay]) sink(session, ev)
    },
    subscribe(sink, phase = 'relay') {
      return register(phase === 'watch' ? watch : relay, sink)
    },
  }
}

// ── The ws module seam (duck-typed: tests inject a fake; the container wires
// the real one through loadWs).

export interface WsSocketLike {
  send(data: string): void
  close(): void
  /** Bytes queued toward a slow consumer (ws exposes bufferedAmount; a fake
   * may omit it and never trip the bound). */
  bufferedAmount?: number
  on(event: 'message', cb: (raw: unknown) => void): unknown
  on(event: 'close', cb: () => void): unknown
  on(event: 'error', cb: (err: unknown) => void): unknown
}

export interface WsServerLike {
  on(event: 'connection', cb: (socket: WsSocketLike) => void): unknown
  handleUpgrade(
    req: http.IncomingMessage, socket: unknown, head: Buffer,
    cb: (socket: WsSocketLike) => void,
  ): void
  close(): void
}

export type WsCtor = new (opts: { noServer: true }) => WsServerLike

/** Render the loadWs failure line: every resolution error, then the
 * consequence of serving no channel. Pure and exported because the failure
 * itself is not drivable end-to-end (attempt 1 resolves inside the image),
 * so the rendered line is pinned directly on this helper
 * (test/ws-events.test.ts). */
export function wsUnresolvableMessage(errors: string[]): string {
  return `[lore-driver] ws unresolvable (${errors.join(' | ')}) `
    + '— the standing event channel is OFF; /followup refuses every turn (503)'
}

/** Resolve the `ws` module — see the module ARCH. The plugin's own location
 * first (a future image may link it into the workspace), then the DSH
 * profile fallback. null when neither resolves — the caller serves no
 * channel and every /followup turn is refused with 503. */
export function loadWs(home: string | undefined): WsCtor | null {
  const attempts: Array<{ where: string; load: () => unknown }> = [
    { where: 'plugin node_modules walk', load: () => createRequire(import.meta.url)('ws') },
    {
      where: 'DSH profile fallback',
      load: () => createRequire(join(home ?? process.cwd(), 'profiles', 'ws-anchor.cjs'))('ws'),
    },
  ]
  const errors: string[] = []
  for (const attempt of attempts) {
    try {
      const mod = attempt.load() as { WebSocketServer?: WsCtor; Server?: WsCtor }
      const ctor = mod.WebSocketServer ?? mod.Server
      if (ctor) return ctor
      errors.push(`${attempt.where}: no server export`)
    } catch (err) {
      errors.push(`${attempt.where}: ${String(err)}`)
    }
  }
  console.error(wsUnresolvableMessage(errors))
  return null
}

// ── The descendant heartbeat.

/** At most one `lore/child-activity` per driving session per window — well
 * inside the backend's 300s silence grace, and a busy child (dozens of
 * searches and fetches) does not flood the socket. */
export const CHILD_ACTIVITY_THROTTLE_MS = 30_000

export interface ChildActivityState {
  /** childId → parentId, filled from each descendant's `header.parentSession`. */
  parents: Map<string, string>
  /** root (driving) session id → when its last heartbeat was due. */
  lastBeat: Map<string, number>
}

export function newChildActivityState(): ChildActivityState {
  return { parents: new Map(), lastBeat: new Map() }
}

/**
 * The driving (root) session a descendant's event should heartbeat, or null:
 * null for a session with no `header.parentSession` (it is a driving session
 * itself — its own frames are its progress) and for a root still inside its
 * throttle window. Nesting resolves through the module-held parent map to the
 * TOP session. Pure — the caller owns the state and the clock.
 */
export function childActivityRoot(
  session: unknown, state: ChildActivityState, now: number,
  throttleMs: number = CHILD_ACTIVITY_THROTTLE_MS,
): string | null {
  const s = session as { id?: unknown; header?: { parentSession?: unknown } } | null
  const id = String(s?.id ?? '')
  const parent = s?.header?.parentSession
  if (!id || typeof parent !== 'string' || !parent) return null
  state.parents.set(id, parent)
  let root = parent
  // Bounded walk: a cyclic parent map (never written by dsh) cannot hang the tap.
  for (let hops = 0; hops < 64; hops += 1) {
    const up = state.parents.get(root)
    if (up === undefined) break
    root = up
  }
  const last = state.lastBeat.get(root)
  if (last !== undefined && now - last < throttleMs) return null
  state.lastBeat.set(root, now)
  return root
}

// ── The channel.

/** The raw duplex socket the upgrade arrives on (only the refusal write
 * needs it — a successful upgrade hands it to the ws server). */
export interface UpgradeSocket {
  /** Flush one chunk and half-close — the refusal must reach the client as
   * a complete status line, not a bare TCP reset. */
  end(chunk: string): void
}

export interface UpgradeServer {
  on(event: 'upgrade', cb: (req: http.IncomingMessage, socket: UpgradeSocket, head: Buffer) => void): unknown
  off?(event: 'upgrade', cb: (req: http.IncomingMessage, socket: UpgradeSocket, head: Buffer) => void): unknown
}

export interface EventsChannelOpts {
  /** The plugin's HTTP server — the upgrade rides it (no port of its own). */
  server: UpgradeServer
  tap: SessionEventTap
  /** lore session id → current dsh session id (SessionMap.get's fallback
   * passes a dsh id through unchanged). */
  resolve(sessionId: string): string
  /** The same header gate the HTTP endpoints use. */
  authorized(req: http.IncomingMessage): boolean
  ws: WsCtor
}

export interface EventsChannel {
  close(): void
  /** One driver-emitted turn-lifecycle frame (model_update, context_usage,
   * error, dsh_stream), addressed by dsh session id: delivered to every
   * subscriber of that session WITHOUT touching their map state — these
   * frames are not dsh log events (no seq, never window-keyed, never
   * replayed). Ordering contract: call from a 'watch'-phase sink to land the
   * frame BEFORE the same event's mapped frames (the wire order). */
  push(dshId: string, frame: Record<string, unknown>): void
}

/** Wire `/ws/events` onto the existing server — see the module doc. */
export function attachEventsChannel(opts: EventsChannelOpts): EventsChannel {
  const { server, tap, resolve, authorized, ws } = opts
  const wss = new ws({ noServer: true })
  // One subscription table per socket: dsh session id → its map state (the
  // module INVARIANT). The table dies with the socket.
  const clients = new Map<WsSocketLike, Map<string, TurnMapState>>()
  // A slow consumer must not buffer without bound: past the byte bound the
  // socket is dropped — the consumer resyncs from its last delivered seq
  // through the projection when it reconnects (the plan's backpressure rule:
  // drop + resync, never reorder).
  const SEND_BUFFER_BOUND = 4 * 1024 * 1024

  const deliver = (client: WsSocketLike, sid: string, frames: Record<string, unknown>[]): void => {
    if ((client.bufferedAmount ?? 0) > SEND_BUFFER_BOUND) {
      console.error(
        '[lore-driver] ws subscriber past the send buffer bound '
        + '— dropping it (it resyncs from its last seq on reconnect)')
      client.close()
      clients.delete(client)
      return
    }
    // Enveloped per session: the frame itself carries no session
    // attribution ("the listener knows; the event does not", map.ts), and a
    // shared socket subscribed to many sessions could not route a bare
    // frame. The envelope is transport addressing; the frame inside stays
    // mapEvent verbatim.
    for (const frame of frames) {
      client.send(JSON.stringify({ type: 'session_frame', session_id: sid, frame }))
    }
  }

  const onUpgrade = (req: http.IncomingMessage, socket: UpgradeSocket, head: Buffer): void => {
    if (!(req.url ?? '').startsWith('/ws/events')) {
      socket.end('HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n')
      return
    }
    if (!authorized(req)) {
      socket.end('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n')
      return
    }
    wss.handleUpgrade(req, socket, head, (client) => wire(client))
  }
  server.on('upgrade', onUpgrade)

  function wire(client: WsSocketLike): void {
    const subs = new Map<string, TurnMapState>()
    clients.set(client, subs)
    // Handled transport errors must not become an unhandled 'error' event
    // (which throws and can take the driver down); logged, and the socket's
    // own close path cleans the table.
    client.on('error', (err) => { console.error('[lore-driver] ws client error:', err) })
    client.on('close', () => { clients.delete(client) })
    client.on('message', (raw) => {
      let msg: unknown
      try {
        msg = JSON.parse(String(raw))
      } catch {
        client.send(JSON.stringify({ type: 'error', message: 'malformed JSON frame' }))
        return
      }
      const m = msg as { type?: unknown; session_id?: unknown }
      if (typeof m.session_id === 'string' && m.session_id
        && (m.type === 'subscribe' || m.type === 'unsubscribe')) {
        const dshId = resolve(m.session_id)
        if (m.type === 'subscribe') {
          subs.set(dshId, newTurnMapState())
          client.send(JSON.stringify({
            type: 'subscribed', session_id: m.session_id, dsh_session_id: dshId,
          }))
        } else {
          subs.delete(dshId)
          client.send(JSON.stringify({ type: 'unsubscribed', session_id: m.session_id }))
        }
        return
      }
      client.send(JSON.stringify({
        type: 'error',
        message: 'expected {"type":"subscribe"|"unsubscribe","session_id":"…"}',
      }))
    })
  }

  const childActivity = newChildActivityState()

  const offTap = tap.subscribe((session, ev) => {
    const sid = String((session as { id?: unknown })?.id ?? '')
    if (!sid) return
    // WHY: dsh writes nothing to a parent's log while its subagents run, so
    // the backend's silence budget would kill a parent whose children are
    // working. A descendant's event heartbeats its ROOT's subscribers —
    // unsequenced, no payload (the child's own frames still emit nothing,
    // map.ts ARCH); a child that stops logging stops the heartbeat, so a hung
    // child is still caught by the grace.
    const root = childActivityRoot(session, childActivity, Date.now())
    if (root !== null) {
      for (const [client, subs] of clients) {
        if (subs.has(root)) deliver(client, root, [{ type: 'lore/child-activity' }])
      }
    }
    // Deleting during iteration is this Map's own supported mutation.
    for (const [client, subs] of clients) {
      const state = subs.get(sid)
      if (!state) continue
      deliver(client, sid, mapEvent(ev as DshEvent, state, { sessionId: sid, isChild: false }))
    }
  }, 'relay')

  return {
    push(dshId: string, frame: Record<string, unknown>) {
      for (const [client, subs] of clients) {
        if (!subs.has(dshId)) continue
        deliver(client, dshId, [frame])
      }
    },
    close() {
      offTap()
      server.off?.('upgrade', onUpgrade)
      wss.close()
    },
  }
}

// ── The assistant-stream relay.

/**
 * The `agent/assistant-stream` sink: one live chunk publication → one
 * `{type:'dsh_stream', frame}` pushed through the standing channel, addressed
 * by the EMITTING agent's session id and otherwise untouched (the browser
 * owns the transient fold — dsh's own client contract, see its
 * ClientAssistantStream). The same frame also feeds the session's reload
 * baseline (`baselines.accept` — stream-baselines.ts): a reload mid-step
 * reads the fold back through /session-entries' `assistant_stream`. apply()
 * registers this on ctx.on beside the session/event tap; a child (subagent)
 * session's publications address no subscriber (the backend subscribes to
 * driving sessions only) and so emit nothing, the same silence map.ts gives
 * child LOG events.
 */
export function relayAssistantStream(
  channel: EventsChannel,
  baselines: SessionStreamBaselines,
): (payload: { agent: unknown; frame: unknown }) => void {
  return ({ agent, frame }) => {
    const sid = String(
      (agent as { session?: { id?: unknown } } | null | undefined)?.session?.id ?? '')
    if (!sid) return
    baselines.accept(sid, frame)
    channel.push(sid, { type: 'dsh_stream', frame })
  }
}

/**
 * The standing event channel: `/ws/events` delivers, per subscribed session,
 * the SAME frames map.ts mints — one mapEvent, one set of mints — so the
 * identity assertions here are against an independent live mapping, never
 * against a fixture copy. The ws module itself is injected (a fake here):
 * the real one resolves only inside the harness image (see ws-events.ts's
 * loadWs ARCH).
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  attachEventsChannel, childActivityRoot, CHILD_ACTIVITY_THROTTLE_MS,
  createSessionEventTap, newChildActivityState, relayAssistantStream,
  wsUnresolvableMessage, type WsCtor, type WsSocketLike,
} from '../src/ws-events.ts'
import { createSessionStreamBaselines } from '../src/stream-baselines.ts'
import { mapEvent, newTurnMapState } from '../src/map.ts'

function ev(seq: number, type: string, data?: any): any {
  return { seq, type, data, time: 1_700_000_000_000 + seq }
}

function assistantMessage(seq: number, text: string): any {
  // Real v4 log shape: the settled `assistant/message` carries the whole step
  // text (live deltas ride the agent/assistant-stream tap, see
  // relayAssistantStream below).
  return ev(seq, 'assistant/message', {
    turn: 1, step: 1,
    message: {
      id: `m${seq}`, role: 'assistant',
      content: [{ type: 'text', text }],
      source: { kind: 'model', provider: 'lore', model: 'x' },
    },
    stream: [],
  })
}

// ── Fakes: the ws impl is injected, so the tests drive the protocol, not the
// socket library. FakeSocket doubles as the client side (receive/closeNow).

class FakeSocket {
  sent: any[] = []
  handlers = new Map<string, Array<(arg?: any) => void>>()
  closed = false
  send(data: string): void { this.sent.push(JSON.parse(data)) }
  close(): void { this.closed = true }
  on(event: string, cb: (arg?: any) => void): void {
    this.handlers.set(event, [...(this.handlers.get(event) ?? []), cb])
  }
  /** Test side: one client → server text frame. */
  receive(msg: unknown): void {
    const raw = typeof msg === 'string' ? msg : JSON.stringify(msg)
    for (const cb of this.handlers.get('message') ?? []) cb(raw)
  }
  /** Test side: the transport dropped the connection. */
  drop(): void {
    for (const cb of this.handlers.get('close') ?? []) cb()
  }
}

class FakeWss {
  static last: FakeWss | null = null
  handleUpgradeCalls = 0
  pending: Array<(socket: WsSocketLike) => void> = []
  closed = false
  on(_event: 'connection', _cb: (socket: WsSocketLike) => void): void {}
  handleUpgrade(
    _req: unknown, _socket: unknown, _head: Buffer,
    done: (socket: WsSocketLike) => void,
  ): void {
    this.handleUpgradeCalls += 1
    this.pending.push(done)
  }
  close(): void { this.closed = true }
}

function fakeWs(): WsCtor {
  FakeWss.last = null
  return class extends FakeWss {
    constructor(_opts: { noServer: true }) {
      super()
      FakeWss.last = this
    }
  } as unknown as WsCtor
}

function fakeServer() {
  const listeners = new Map<string, (arg?: any) => void>()
  return {
    on: (event: string, cb: (arg?: any) => void) => { listeners.set(`on:${event}`, cb) },
    off: (event: string, _cb: unknown) => { listeners.delete(`on:${event}`) },
    upgrade: (req: any, socket: any) => {
      const cb = listeners.get('on:upgrade')
      if (cb) (cb as any)(req, socket, Buffer.alloc(0))
    },
  }
}

const SECRET = 's3cr3t'
function channelFixture() {
  const tap = createSessionEventTap()
  const server = fakeServer()
  const channel = attachEventsChannel({
    server: server as any,
    tap,
    // The identity map's own fallback: a lore id resolves, a dsh id passes
    // through unchanged.
    resolve: (id: string) => (id === 'lore-1' ? 'dsh-9' : id),
    authorized: (req: any) => req.headers['x-driver-secret'] === SECRET,
    ws: fakeWs(),
  })
  return { tap, server, channel }
}

/** Complete one authorized upgrade → the wired client socket. */
function accept(server: ReturnType<typeof fakeServer>, wss: FakeWss): FakeSocket {
  const before = wss.handleUpgradeCalls
  server.upgrade(
    { url: '/ws/events', headers: { 'x-driver-secret': SECRET } },
    { end() {} },
  )
  assert.equal(wss.handleUpgradeCalls, before + 1, 'the upgrade reached the ws server')
  const socket = new FakeSocket()
  ;(wss.pending.shift() as (s: WsSocketLike) => void)(socket)
  return socket
}

test('subscribe acks with the resolved dsh id, then delivers the SAME frames mapEvent mints', () => {
  const { tap, server } = channelFixture()
  const socket = accept(server, FakeWss.last!)

  socket.receive({ type: 'subscribe', session_id: 'lore-1' })
  assert.deepEqual(socket.sent[0], {
    type: 'subscribed', session_id: 'lore-1', dsh_session_id: 'dsh-9',
  })

  // The independent mapping a live consumer would run for the same events —
  // the identity the channel must not diverge from.
  const live = newTurnMapState()
  const expected: Record<string, unknown>[] = []
  const events = [
    ev(1, 'turn/start', { turn: 3 }),
    assistantMessage(2, 'hi'),
    ev(3, 'turn/end', { turn: 3, reason: { kind: 'aborted' } }), // mints lore/halt
  ]
  for (const e of events) {
    expected.push(...mapEvent(e, live, { sessionId: 'dsh-9', isChild: false }))
  }

  for (const e of events) tap.emit({ id: 'dsh-9' }, e)
  // Enveloped per session: the frame itself carries no session
  // attribution ("the listener knows; the event does not", map.ts), so the
  // shared socket addresses it — the frame inside stays mapEvent verbatim.
  assert.deepEqual(socket.sent.slice(1), expected.map((frame) => ({
    type: 'session_frame', session_id: 'dsh-9', frame,
  })))
})

test('unsubscribe stops delivery and acks', () => {
  const { tap, server } = channelFixture()
  const socket = accept(server, FakeWss.last!)
  socket.receive({ type: 'subscribe', session_id: 'dsh-9' })
  socket.receive({ type: 'unsubscribe', session_id: 'dsh-9' })
  assert.deepEqual(socket.sent[1], { type: 'unsubscribed', session_id: 'dsh-9' })

  const before = socket.sent.length
  tap.emit({ id: 'dsh-9' }, ev(4, 'turn/start', { turn: 4 }))
  assert.equal(socket.sent.length, before, 'no frame after unsubscribe')
})

test("another session's events reach no subscriber", () => {
  const { tap, server } = channelFixture()
  const socket = accept(server, FakeWss.last!)
  socket.receive({ type: 'subscribe', session_id: 'dsh-9' })
  socket.sent.length = 0

  tap.emit({ id: 'dsh-other' }, ev(1, 'turn/start', { turn: 1 }))
  tap.emit({ id: '' }, ev(2, 'turn/start', { turn: 1 }))
  assert.deepEqual(socket.sent, [])
})

test('one state per (socket, session): the turn coordinate tracks the open turn', () => {
  const { tap, server } = channelFixture()
  const socket = accept(server, FakeWss.last!)
  socket.receive({ type: 'subscribe', session_id: 'dsh-9' })

  tap.emit({ id: 'dsh-9' }, ev(1, 'turn/start', { turn: 2 }))
  tap.emit({ id: 'dsh-9' }, ev(2, 'turn/end', { turn: 2, reason: { kind: 'completed' } }))
  tap.emit({ id: 'dsh-9' }, ev(3, 'turn/start', { turn: 5 }))
  tap.emit({ id: 'dsh-9' }, ev(4, 'approval/asked', { callId: 'c1', toolName: 'edit_document' }))

  const mint = socket.sent.find((f) => f.frame?.type === 'lore/verdict-ask')
  assert.ok(mint, 'the ask minted')
  // turn 5, not 2 — a state seeded once and never re-seeded per turn would
  // carry a stale coordinate into every later turn's mints.
  assert.equal(mint.frame.data.turn, 5)
})

// ── The descendant heartbeat: a child's events keep its ROOT's turn alive.

/** A session object the way dsh hands it to the tap: a child carries its
 * parent's id in `header.parentSession`; a driving session carries none. */
function sessionOf(id: string, parentSession?: string): any {
  return { id, header: parentSession === undefined ? {} : { parentSession } }
}

const HEARTBEAT = { type: 'session_frame', session_id: 'dsh-9', frame: { type: 'lore/child-activity' } }

test("a child's event reaches the ROOT's subscriber as one lore/child-activity, never as its own frames", () => {
  const { tap, server } = channelFixture()
  const socket = accept(server, FakeWss.last!)
  socket.receive({ type: 'subscribe', session_id: 'dsh-9' })
  socket.sent.length = 0

  tap.emit(sessionOf('child-1', 'dsh-9'), ev(1, 'turn/start', { turn: 1 }))
  assert.deepEqual(socket.sent, [HEARTBEAT])
})

test('a grandchild resolves to the top driving session', () => {
  const { tap, server } = channelFixture()
  const socket = accept(server, FakeWss.last!)
  socket.receive({ type: 'subscribe', session_id: 'dsh-9' })
  socket.sent.length = 0

  // The child's header registers child-1 → dsh-9; the grandchild resolves
  // through it (throttled away here: one window per root).
  tap.emit(sessionOf('child-1', 'dsh-9'), ev(1, 'turn/start', { turn: 1 }))
  const state = newChildActivityState()
  childActivityRoot(sessionOf('child-1', 'dsh-9'), state, 0)
  assert.equal(
    childActivityRoot(sessionOf('grand-1', 'child-1'), state, CHILD_ACTIVITY_THROTTLE_MS),
    'dsh-9')
  assert.deepEqual(socket.sent, [HEARTBEAT])
})

test('a second child event inside the window is throttled; past it, it beats again', () => {
  const { tap, server } = channelFixture()
  const socket = accept(server, FakeWss.last!)
  socket.receive({ type: 'subscribe', session_id: 'dsh-9' })
  socket.sent.length = 0

  tap.emit(sessionOf('child-1', 'dsh-9'), ev(1, 'tool/call', {}))
  tap.emit(sessionOf('child-2', 'dsh-9'), ev(1, 'tool/call', {}))
  assert.deepEqual(socket.sent, [HEARTBEAT], 'one heartbeat per root per window')

  const state = newChildActivityState()
  assert.equal(childActivityRoot(sessionOf('c', 'dsh-9'), state, 1_000), 'dsh-9')
  assert.equal(childActivityRoot(sessionOf('c', 'dsh-9'), state, 1_000 + CHILD_ACTIVITY_THROTTLE_MS - 1), null)
  assert.equal(childActivityRoot(sessionOf('c', 'dsh-9'), state, 1_000 + CHILD_ACTIVITY_THROTTLE_MS), 'dsh-9')
})

test('a child of an unsubscribed root delivers nothing; a driving session is no child', () => {
  const { tap, server } = channelFixture()
  const socket = accept(server, FakeWss.last!)
  socket.receive({ type: 'subscribe', session_id: 'dsh-9' })
  socket.sent.length = 0

  tap.emit(sessionOf('child-x', 'dsh-other'), ev(1, 'turn/start', { turn: 1 }))
  assert.deepEqual(socket.sent, [])
  assert.equal(childActivityRoot(sessionOf('dsh-9'), newChildActivityState(), 0), null)
})

test('an unauthorized upgrade is refused before the handshake', () => {
  const { server } = channelFixture()
  const ended: string[] = []
  server.upgrade(
    { url: '/ws/events', headers: { 'x-driver-secret': 'wrong' } },
    { end(chunk: string) { ended.push(chunk) } },
  )
  assert.ok(ended[0].startsWith('HTTP/1.1 401'), ended[0])
  assert.equal(FakeWss.last!.handleUpgradeCalls, 0, 'no handshake on a refused upgrade')
})

test('a non-/ws/events upgrade is 404', () => {
  const { server } = channelFixture()
  const ended: string[] = []
  server.upgrade(
    { url: '/other', headers: { 'x-driver-secret': SECRET } },
    { end(chunk: string) { ended.push(chunk) } },
  )
  assert.ok(ended[0].startsWith('HTTP/1.1 404'), ended[0])
  assert.equal(FakeWss.last!.handleUpgradeCalls, 0)
})

test('a malformed client frame gets an error frame; the subscription lives on', () => {
  const { tap, server } = channelFixture()
  const socket = accept(server, FakeWss.last!)
  socket.receive('{not json')
  socket.receive({ type: 'subscribe' }) // missing session_id
  const errors = socket.sent.filter((f) => f.type === 'error')
  assert.equal(errors.length, 2)

  socket.receive({ type: 'subscribe', session_id: 'dsh-9' })
  socket.sent.length = 0
  tap.emit({ id: 'dsh-9' }, ev(1, 'turn/start', { turn: 1 }))
  assert.ok(socket.sent.length > 0, 'the socket still delivers after protocol errors')
})

test('a dropped socket stops receiving and its subscriptions die with it', () => {
  const { tap, server } = channelFixture()
  const socket = accept(server, FakeWss.last!)
  socket.receive({ type: 'subscribe', session_id: 'dsh-9' })
  socket.drop()
  socket.sent.length = 0

  tap.emit({ id: 'dsh-9' }, ev(2, 'turn/start', { turn: 1 }))
  assert.deepEqual(socket.sent, [])
})

test('a subscriber past the send buffer bound is dropped, not buffered forever', () => {
  const { tap, server } = channelFixture()
  const socket = accept(server, FakeWss.last!)
  // A slow consumer: ws keeps buffering unsent bytes past any sane bound.
  ;(socket as any).bufferedAmount = 5 * 1024 * 1024
  socket.receive({ type: 'subscribe', session_id: 'dsh-9' })
  tap.emit({ id: 'dsh-9' }, ev(1, 'turn/start', { turn: 1 }))
  assert.ok(socket.closed, 'the socket is dropped')
  assert.deepEqual(socket.sent.filter((f) => f.frame?.type === 'dsh_event'), [],
    'no frame is queued toward the saturated consumer (drop + resync, never reorder)')
})

test('push() delivers a driver-addressed frame to subscribers of that session only', () => {
  const { tap, server, channel } = channelFixture()
  const owner = accept(server, FakeWss.last!)
  owner.receive({ type: 'subscribe', session_id: 'dsh-9' })
  const other = accept(server, FakeWss.last!)
  other.receive({ type: 'subscribe', session_id: 'dsh-other' })
  owner.sent.length = 0
  other.sent.length = 0

  channel.push('dsh-9', { type: 'model_update', model: 'x' })
  assert.deepEqual(owner.sent, [{
    type: 'session_frame', session_id: 'dsh-9', frame: { type: 'model_update', model: 'x' },
  }])
  assert.deepEqual(other.sent, [], 'a driver frame is addressed, never broadcast')
  // The push never touched the map state: the next event still maps.
  tap.emit({ id: 'dsh-9' }, ev(1, 'turn/start', { turn: 1 }))
  assert.equal(owner.sent[1].frame.type, 'dsh_event')
})

test("a watch-phase sink's pushes land BEFORE the same event's mapped frames", () => {
  // The followup runner's turn watcher: the turn-end context_usage
  // must reach subscribers before the terminal relay and its halt mint — the
  // wire order (the backend captures cu at turn finalization; after the
  // terminal frame it is too late).
  const { tap, server, channel } = channelFixture()
  const socket = accept(server, FakeWss.last!)
  socket.receive({ type: 'subscribe', session_id: 'dsh-9' })
  socket.sent.length = 0
  tap.subscribe((_session, ev) => {
    if ((ev as any).type === 'turn/end') {
      channel.push('dsh-9', { type: 'context_usage', used: 1, cap: 2 })
    }
  }, 'watch')

  tap.emit({ id: 'dsh-9' }, ev(1, 'turn/end', { turn: 1, reason: { kind: 'aborted' } }))
  assert.deepEqual(socket.sent.map((f) => f.frame.type), ['context_usage', 'dsh_event', 'lore/halt'],
    'context_usage precedes the terminal relay (and its halt mint)')
})

// ── repoint: a fork re-keys live subscriptions ──────────────────────────
// The plugin owns the repoint because it owns both the identity map and
// this table; the subscriber does nothing.

test('repoint re-keys the table: the fresh id delivers, the old one no longer does, and the re-ack carries the tail', () => {
  const { tap, server, channel } = channelFixture()
  const socket = accept(server, FakeWss.last!)
  socket.receive({ type: 'subscribe', session_id: 'lore-1' }) // table key: dsh-9
  socket.sent.length = 0

  channel.repoint('lore-1', 'dsh-9', 'dsh-b', 40)
  assert.deepEqual(socket.sent, [{
    type: 'subscribed', session_id: 'lore-1', dsh_session_id: 'dsh-b', tail_seq: 40,
  }], 'the re-ack names the lore id, the FRESH dsh id, and the fork tail')
  socket.sent.length = 0

  // The pre-fork id no longer routes; the fresh one does — the forked turn
  // streams to the subscriber that was already there.
  tap.emit({ id: 'dsh-9' }, ev(50, 'turn/start', { turn: 2 }))
  assert.deepEqual(socket.sent, [], 'the pre-fork dsh id no longer delivers')
  tap.emit({ id: 'dsh-b' }, ev(41, 'turn/start', { turn: 2 }))
  assert.equal(socket.sent.length, 1)
  assert.equal(socket.sent[0].session_id, 'dsh-b')
  assert.equal(socket.sent[0].frame.kind, 'turn/start')
})

test('repoint: a socket without the old id is untouched; a root fork re-acks with tail null', () => {
  const { server, channel } = channelFixture()
  const owner = accept(server, FakeWss.last!)
  owner.receive({ type: 'subscribe', session_id: 'lore-1' }) // dsh-9
  const other = accept(server, FakeWss.last!)
  other.receive({ type: 'subscribe', session_id: 'dsh-other' })
  owner.sent.length = 0
  other.sent.length = 0

  channel.repoint('lore-1', 'dsh-9', 'dsh-b', null)
  assert.deepEqual(owner.sent, [{
    type: 'subscribed', session_id: 'lore-1', dsh_session_id: 'dsh-b', tail_seq: null,
  }], 'root fork: the tail is null (the fresh log is empty)')
  assert.deepEqual(other.sent, [],
    'no re-ack to a socket that never held the old id — a later subscribe resolves the fresh id by itself')
})

test('close() detaches the channel from the tap and the server', () => {
  const { tap, server, channel } = channelFixture()
  const socket = accept(server, FakeWss.last!)
  socket.receive({ type: 'subscribe', session_id: 'dsh-9' })
  channel.close()
  socket.sent.length = 0

  tap.emit({ id: 'dsh-9' }, ev(3, 'turn/start', { turn: 1 }))
  assert.deepEqual(socket.sent, [], 'no delivery after close')
  assert.ok(FakeWss.last!.closed, 'the ws server closed')
  // A late upgrade after close must not crash the channel.
  server.upgrade(
    { url: '/ws/events', headers: { 'x-driver-secret': SECRET } },
    { end() {} },
  )
})

// ── The assistant-stream relay: live chunks ride dsh_stream ─────────────
//
// The v4 log holds only settled events: the live tail is dsh's own
// `agent/assistant-stream` publication (transient; the loop appends the final
// assistant/message before the end frame). apply() registers the sink below
// on ctx.on('agent/assistant-stream') and it pushes `{type:'dsh_stream',
// frame}` through the SAME channel — session-addressed, unsequenced, never
// replayed (a resync reads the settled log).

test('an assistant-stream publication relays as dsh_stream to that session only', () => {
  const { server, channel } = channelFixture()
  const owner = accept(server, FakeWss.last!)
  owner.receive({ type: 'subscribe', session_id: 'dsh-9' })
  const other = accept(server, FakeWss.last!)
  other.receive({ type: 'subscribe', session_id: 'dsh-other' })
  owner.sent.length = 0
  other.sent.length = 0

  const onStream = relayAssistantStream(channel, createSessionStreamBaselines())
  const emit = (id: unknown, frame: unknown) => onStream({ agent: { session: { id } }, frame })
  const start = { type: 'start', attemptId: 'a1', revision: 1, turn: 1, step: 1 }
  const chunk = {
    type: 'chunk', attemptId: 'a1', revision: 2, index: 0, time: 5,
    chunk: { type: 'text-delta', index: 0, text: 'hi' },
  }
  emit('dsh-9', start)
  emit('dsh-9', chunk)
  emit('dsh-other', chunk)
  emit('', chunk) // no session id: dropped, never broadcast

  assert.deepEqual(owner.sent.map((f) => f.frame), [
    { type: 'dsh_stream', frame: start },
    { type: 'dsh_stream', frame: chunk },
  ])
  assert.deepEqual(other.sent.map((f) => f.frame), [{ type: 'dsh_stream', frame: chunk }])
})

test('a stream chunk lands on the wire before the same attempt\'s settlement', () => {
  // The runtime publishes chunk frames during the request and appends the
  // final assistant/message to the log at settlement; the sink pushes at
  // publish time, so the transient rows reach the browser BEFORE the
  // settlement's session event — dsh's own client order.
  const { tap, server, channel } = channelFixture()
  const socket = accept(server, FakeWss.last!)
  socket.receive({ type: 'subscribe', session_id: 'dsh-9' })
  socket.sent.length = 0

  const onStream = relayAssistantStream(channel, createSessionStreamBaselines())
  onStream({
    agent: { session: { id: 'dsh-9' } },
    frame: {
      type: 'chunk', attemptId: 'a1', revision: 1, index: 0, time: 5,
      chunk: { type: 'text-delta', index: 0, text: 'hi' },
    },
  })
  tap.emit({ id: 'dsh-9' }, assistantMessage(1, 'hi'))

  assert.deepEqual(socket.sent.map((f) => f.frame.type), ['dsh_stream', 'dsh_event'])
})

test('the relay also folds each frame into the session\'s reload baseline', () => {
  // The same sink that pushes dsh_stream feeds dsh's own
  // SessionAssistantStreamAccumulator keyed by session, with
  // the session's last observed event seq as the durable cursor —
  // /session-entries serves the fold's snapshot on the open turn, so a reload
  // mid-step keeps the streamed text. A revision gap (a missed frame) resets
  // the fold: the snapshot then carries NO active attempt — no baseline is
  // served, never a wrong one.
  const { channel } = channelFixture()
  const baselines = createSessionStreamBaselines()
  const onStream = relayAssistantStream(channel, baselines)
  baselines.observe('dsh-9', 4)
  // Revisions are DENSE across frames (agent-loop allocates each from one
  // counter): start=1, chunk=2, … — the fold's own contract.
  onStream({
    agent: { session: { id: 'dsh-9' } },
    frame: { type: 'start', attemptId: 'a1', revision: 1, turn: 1, step: 1 },
  })
  onStream({
    agent: { session: { id: 'dsh-9' } },
    frame: {
      type: 'chunk', attemptId: 'a1', revision: 2, index: 0, time: 5,
      chunk: { type: 'text-delta', index: 0, text: 'hi' },
    },
  })
  let snap = baselines.snapshot('dsh-9')!
  assert.equal(snap.activeAttempt?.attemptId, 'a1')
  assert.equal(snap.activeAttempt?.startedAfterSeq, 4)
  assert.equal(snap.activeAttempt?.nextIndex, 1)
  assert.deepEqual(snap.activeAttempt?.stream, [
    { type: 'text-chunks', time0: 5, index: 0, dt: [], texts: ['hi'] },
  ])
  // A revision gap (a missed frame) resets the fold: the attempt is gone.
  onStream({
    agent: { session: { id: 'dsh-9' } },
    frame: {
      type: 'chunk', attemptId: 'a1', revision: 4, index: 1, time: 6,
      chunk: { type: 'text-delta', index: 0, text: '!' },
    },
  })
  snap = baselines.snapshot('dsh-9')!
  assert.equal(snap.activeAttempt, undefined)
  // An unknown session serves no snapshot at all.
  assert.equal(baselines.snapshot('dsh-none'), undefined)
})

// ── loadWs's failure line ────────────────────────────────────────────────
// The composition where `ws` cannot resolve is not drivable end-to-end
// (attempt 1 resolves inside the image — dsh's node_modules is one walk up),
// so the rendered line is pinned directly on the pure helper that builds it.

test('wsUnresolvableMessage renders every resolution error and the 503 consequence', () => {
  const line = wsUnresolvableMessage(['a: x', 'b: y'])
  assert.ok(line.includes('(a: x | b: y)'), `the errors join verbatim: ${line}`)
  assert.match(line, /the standing event channel is OFF; \/followup refuses every turn \(503\)/,
    'the consequence the operator reads when the channel is off')
  assert.ok(line.startsWith('[lore-driver] ws unresolvable'), line)
})

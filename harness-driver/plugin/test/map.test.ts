/**
 * The dsh→frame RELAY contract (plan lore-renders-dsh-conversation step 3).
 * map.ts NO LONGER TRANSLATES: every dsh event emits VERBATIM as a neutral
 * `{type:'dsh_event', kind, seq, time, data, surfaceOp?, sourceEventSeqs?}`
 * frame — the event's transport to the browser's assembler, not a view of it
 * — and the ONE lore mint anchored inside a dsh session event
 * (`lore/verdict-ask` on `approval/asked`) rides beside it. Visibility is the
 * assembler's node definitions' decision; the relay has no hide list.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { mapEvent, newTurnMapState, toRelayFrame, type DshEvent } from '../src/map.ts'

function ev(seq: number, type: string, data?: any, surfaceOp?: any): any {
  return { seq, type, data, time: 1_700_000_000_000 + seq, surfaceOp }
}

function toolCall(seq: number, callId: string, name: string, args: unknown): any {
  return ev(seq, 'tool/call', {
    turn: 1, step: 1, callId, name, arguments: JSON.stringify(args),
  })
}

function toolResult(seq: number, callId: string, text: string, isError = false): any {
  return ev(seq, 'tool/result', {
    turn: 1, step: 1,
    message: {
      source: { kind: 'tool', callId },
      content: [{
        type: 'tool-result', toolCallId: callId, isError,
        content: [{ type: 'text', text }],
      }],
    },
  }, 'append')
}

// ─── the verbatim relay ───────────────────────────────────────────────────────

test('every dsh event relays verbatim: kind, seq, time, data, surface metadata', () => {
  const checkpoint = ev(4, 'user/message', {
    role: 'user', content: [{ type: 'text', text: 'checkpoint' }],
  }, { op: 'replace', startSeq: 1, endSeq: 3 })
  checkpoint.sourceEventSeqs = [1, 2, 3]
  const samples: DshEvent[] = [
    ev(1, 'assistant/chunk', { turn: 1, step: 1, chunk: { type: 'text-delta', text: 'Hi' } }),
    toolCall(2, 'call-1', 'read_document', { document_id: 'd1' }),
    toolResult(3, 'call-1', '(ok)'),
    checkpoint,
    ev(5, 'hook/invoked', { hook: 'x' }),
    ev(6, 'session/title', { title: 'A chat', messageSeqs: [1], source: 'model' }),
    ev(7, 'compaction/end', { compactionId: 'cpt-abc', turn: 1 }),
    ev(8, 'turn/start', { turn: 1 }),
    ev(9, 'turn/end', { turn: 1, reason: { kind: 'completed' } }),
  ]
  for (const sample of samples) {
    assert.deepEqual(
      mapEvent(sample, newTurnMapState()),
      [toRelayFrame(sample)],
      `${sample.type} must relay verbatim — the translator is gone`,
    )
  }
})

test('the relay frame is the event under kind+seq+time+data — no truncation, no derivation', () => {
  const fat = 'x'.repeat(100_000)
  const frame = toRelayFrame(ev(1, 'tool/result', { message: { content: [{ type: 'text', text: fat }] } }))
  assert.equal((frame as any).kind, 'tool/result')
  assert.equal((frame as any).seq, 1)
  assert.equal((frame as any).time, 1_700_000_000_001)
  // The assembler rebuilds the event from this data: truncation would corrupt
  // the assembled view, so the old wire bound is gone with the chip it fed.
  assert.equal(JSON.stringify((frame as any).data).length, JSON.stringify({ message: { content: [{ type: 'text', text: fat }] } }).length)
})

test('turn/start records the turn coordinate; turn/end marks the turn finished', () => {
  const st = newTurnMapState()
  mapEvent(ev(1, 'turn/start', { turn: 3 }), st)
  assert.equal(st.turn, 3)
  mapEvent(ev(2, 'approval/asked', { callId: 'c1', toolName: 'edit_document' }), st)
  assert.equal(st.finished, false)
  mapEvent(ev(9, 'turn/end', { turn: 3, reason: { kind: 'completed' } }), st)
  assert.equal(st.finished, true)
})

// ─── the one lore mint anchored in a dsh session event ────────────────────────

test('approval/asked mints lore/verdict-ask at the ask seq + 0.5', () => {
  const st = newTurnMapState()
  mapEvent(ev(1, 'turn/start', { turn: 2 }), st)
  const frames = mapEvent(ev(7, 'approval/asked', { callId: 'call-edit', toolName: 'edit_document' }), st)
  assert.equal(frames.length, 1)
  const mint: any = frames[0]
  assert.equal(mint.type, 'lore/verdict-ask')
  // The placement PROTOCOL (lore-events.ts): anchor + the kind's offset — the
  // same value the replay mints, so a reload lands at the identical position.
  assert.equal(mint.seq, 7.5)
  assert.equal(mint.ignorable, true)
  assert.deepEqual(mint.data, { turn: 2, callId: 'call-edit', toolName: 'edit_document' })
})

test('a malformed ask relays verbatim instead of minting a broken card', () => {
  const st = newTurnMapState()
  const frames = mapEvent(ev(7, 'approval/asked', { toolName: 'edit_document' }), st)
  assert.equal(frames.length, 1)
  assert.equal((frames[0] as any).type, 'dsh_event')
})

test('turn/end with a reason dsh renders no node for mints lore/halt at +0.7', () => {
  // aborted / blocked / interrupted (and an unknown reason): dsh has no node,
  // so the relay mints the halt card — the SAME mint the replay emits, and the
  // same sentences the retired translator carried (HALT_MESSAGES).
  for (const [kind, message] of [
    ['aborted', 'The turn was cancelled.'],
    ['blocked', 'The turn was blocked before a model step.'],
    ['interrupted', 'The session was recovered after an interruption.'],
    ['made_up_reason', undefined],
  ] as const) {
    const st = newTurnMapState()
    mapEvent(ev(1, 'turn/start', { turn: 3 }), st)
    const frames = mapEvent(ev(9, 'turn/end', { turn: 3, reason: { kind } }), st)
    assert.equal(frames.length, 2, kind)
    assert.equal((frames[0] as any).type, 'dsh_event')
    const mint: any = frames[1]
    assert.equal(mint.type, 'lore/halt')
    assert.equal(mint.seq, 9.7, kind)
    assert.equal(mint.ignorable, true)
    assert.deepEqual(mint.data, {
      turn: 3, reason: kind, ...(message ? { message } : {}),
    }, kind)
  }
})

test('turn/end completed / max-tokens / error mint NO halt (dsh renders them)', () => {
  for (const kind of ['completed', 'max-tokens', 'error']) {
    const st = newTurnMapState()
    const frames = mapEvent(ev(9, 'turn/end', { turn: 1, reason: { kind } }), st)
    assert.equal(frames.length, 1, kind)
    assert.equal((frames[0] as any).type, 'dsh_event', kind)
  }
})

// ─── child sessions emit nothing ──────────────────────────────────────────────

test('a child session event emits NOTHING (its seq space would collide)', () => {
  const origin = { sessionId: 'child-1', isChild: true }
  assert.deepEqual(mapEvent(toolCall(3, 'c', 'read_document', {}), newTurnMapState(), origin), [])
  assert.deepEqual(
    mapEvent(ev(4, 'assistant/message', { turn: 1, step: 1, message: { role: 'assistant', content: [] } }), newTurnMapState(), origin),
    [],
  )
  assert.deepEqual(
    mapEvent(ev(5, 'turn/end', { turn: 1, reason: { kind: 'completed' } }), newTurnMapState(), origin),
    [],
  )
})

// ─── what the old vocabulary minted is now the assembler's decision ──────────

test('the kinds the old translator turned into frames now relay verbatim', () => {
  // assistant/chunk deltas, the settled agent step, the mid-turn verdict card,
  // the terminal frames, the compaction frame, the title frame: every one of
  // them was a translation the browser no longer needs — the assembler's node
  // definitions and the backend's persistence arms read the events directly.
  const st = newTurnMapState()
  for (const sample of [
    ev(1, 'assistant/chunk', { chunk: { type: 'reasoning-delta', text: 'hmm' } }),
    ev(2, 'assistant/chunk', { chunk: { type: 'usage', usage: { inputTokens: 10 } } }),
    toolCall(3, 'call-1', 'generate_image', { prompt: 'x' }),
    toolResult(4, 'call-1', '{"status":"generating","run_id":"r1"}'),
    ev(5, 'session/title', { title: 't' }),
    ev(6, 'compaction/end', { compactionId: 'cpt' }),
    ev(7, 'turn/end', { turn: 1, reason: { kind: 'max-tokens' } }),
  ]) {
    const frames = mapEvent(sample, st)
    assert.equal(frames.length, 1)
    assert.equal((frames[0] as any).type, 'dsh_event', `${sample.type} must not mint a typed frame`)
  }
})

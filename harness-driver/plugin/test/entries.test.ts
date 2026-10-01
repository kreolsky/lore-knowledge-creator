/**
 * The RELOAD half of the one projection (plan lore-renders-dsh-conversation
 * step 3): a session log replays through the SAME mapEvent the live listener
 * uses — verbatim `dsh_event` frames plus the `lore/verdict-ask` mint — so
 * reload and live agree by construction.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { projectSessionEntries } from '../src/entries.ts'
import { mapEvent, newTurnMapState } from '../src/map.ts'

function ev(seq: number, type: string, data?: any, surfaceOp?: any): any {
  return { seq, type, data, time: 1_700_000_000_000 + seq, surfaceOp }
}

function assistantMessage(seq: number, text: string): any {
  // Real v3 shape: the settled message carries the whole step text.
  return ev(seq, 'assistant/message', {
    turn: 1, step: 1,
    message: { role: 'assistant', content: [{ type: 'text', text }] },
  }, 'append')
}

function toolCall(seq: number, callId: string, name: string, args: unknown): any {
  return ev(seq, 'tool/call', {
    turn: 1, step: 1, callId, name, arguments: JSON.stringify(args),
  })
}

function toolResult(seq: number, callId: string, text: string): any {
  return ev(seq, 'tool/result', {
    turn: 1, step: 1,
    message: {
      source: { kind: 'tool', callId },
      content: [{
        type: 'tool-result', toolCallId: callId, isError: false,
        content: [{ type: 'text', text }],
      }],
    },
  }, 'append')
}

test('one turn per turn/start…turn/end pair, frames verbatim', () => {
  const replay = projectSessionEntries([
    ev(1, 'turn/start', { turn: 1 }), assistantMessage(2, 'hi'), ev(3, 'turn/end', { turn: 1, reason: { kind: 'completed' } }),
    ev(4, 'turn/start', { turn: 2 }), assistantMessage(5, 'again'), ev(6, 'turn/end', { turn: 2, reason: { kind: 'completed' } }),
  ])
  assert.equal(replay.turns.length, 2)
  assert.deepEqual(replay.turns[0].frames[0], {
    type: 'dsh_event', kind: 'turn/start', seq: 1, time: 1_700_000_000_001, data: { turn: 1 },
  })
  assert.deepEqual(replay.turns[0].frames[1], {
    type: 'dsh_event', kind: 'assistant/message', seq: 2, time: 1_700_000_000_002,
    data: { turn: 1, step: 1, message: { role: 'assistant', content: [{ type: 'text', text: 'hi' }] } },
    surfaceOp: 'append',
  })
})

test('each replayed turn names its own boundary seq (end_seq)', () => {
  // The read path keys rows to turns by the row's driver_seq stamp — the
  // SAME dsh log seq the fork seam names (plan collapse-the-editor-harness-
  // layer step 5) — so every replayed turn must carry its turn/end's seq.
  const replay = projectSessionEntries([
    ev(1, 'turn/start', { turn: 1 }), assistantMessage(2, 'hi'), ev(3, 'turn/end', { turn: 1, reason: { kind: 'completed' } }),
    ev(4, 'turn/start', { turn: 2 }), assistantMessage(5, 'again'), ev(6, 'turn/end', { turn: 2, reason: { kind: 'completed' } }),
  ])
  assert.equal(replay.turns[0].end_seq, 3)
  assert.equal(replay.turns[1].end_seq, 6)
})

test('the replayed frames ARE the live frames — same mapEvent, same order', () => {
  const log = [
    ev(1, 'turn/start', { turn: 1 }),
    assistantMessage(2, 'thinking… '),
    toolCall(3, 'c1', 'search_materials', { query: 'x' }),
    toolResult(4, 'c1', 'found'),
    assistantMessage(5, 'done'),
    ev(6, 'turn/end', { turn: 1, reason: { kind: 'completed' } }),
  ]
  const live: Record<string, unknown>[] = []
  const state = newTurnMapState()
  for (const e of log) live.push(...mapEvent(e, state))

  const replay = projectSessionEntries(log)
  assert.equal(replay.turns.length, 1)
  assert.deepEqual(replay.turns[0].frames, live)
})

test('an approval/asked inside a turn mints lore/verdict-ask with the turn coordinate', () => {
  const replay = projectSessionEntries([
    ev(1, 'turn/start', { turn: 4 }),
    toolCall(2, 'c1', 'edit_document', {}),
    ev(3, 'approval/asked', { callId: 'c1', toolName: 'edit_document' }),
    toolResult(4, 'c1', 'applied'),
    ev(5, 'turn/end', { turn: 4, reason: { kind: 'completed' } }),
  ])
  const kinds = replay.turns[0].frames.map((f) => (f as any).type)
  assert.deepEqual(kinds, [
    'dsh_event', 'dsh_event', 'lore/verdict-ask', 'dsh_event', 'dsh_event',
  ])
  const mint: any = replay.turns[0].frames[2]
  assert.equal(mint.seq, 3.5)
  assert.deepEqual(mint.data, { turn: 4, callId: 'c1', toolName: 'edit_document' })
})

test('an unfinished trailing turn is EMITTED as an open turn — no end_seq', () => {
  // Plan agent-line-harness-lifecycle step 1: under a driver-owned lifecycle
  // the normal reload case is a mid-turn gap, so the projection may not drop
  // the turn the log never closed. Which record a session RENDERS for it —
  // this open turn or the halt card — is the consumer's split, pinned as the
  // INVARIANT on the emitting branch in entries.ts.
  const replay = projectSessionEntries([
    ev(1, 'turn/start', { turn: 1 }), assistantMessage(2, 'ok'), ev(3, 'turn/end', { turn: 1, reason: { kind: 'completed' } }),
    ev(4, 'turn/start', { turn: 2 }), assistantMessage(5, 'cut off'),
  ])
  assert.equal(replay.turns.length, 2)
  assert.equal(replay.turns[1].end_seq, undefined)
  assert.deepEqual(
    replay.turns[1].frames.map((f) => (f as any).kind),
    ['turn/start', 'assistant/message'],
  )
  // The tail seq still anchors the backend's reload lore/halt mint for a turn
  // the Lore side knows is dead (no terminal frame exists — the window tail does).
  assert.equal(replay.tail_seq, 5)
})

test('since_seq replays only frames past the boundary — verbatim, in order', () => {
  // The resync contract: a consumer that lost the channel resubscribes with
  // its last delivered frame seq and receives exactly the frames above it —
  // the SAME frames (same mapEvent, same state evolution over the whole log),
  // so live-assembled == reloaded by construction, never by re-derivation.
  const log = [
    ev(1, 'turn/start', { turn: 1 }), assistantMessage(2, 'a'), ev(3, 'turn/end', { turn: 1, reason: { kind: 'completed' } }),
    ev(4, 'turn/start', { turn: 2 }), assistantMessage(5, 'b'), ev(6, 'turn/end', { turn: 2, reason: { kind: 'completed' } }),
    ev(7, 'turn/start', { turn: 3 }), assistantMessage(8, 'live tail'),
  ]
  const full = projectSessionEntries(log)
  const resync = projectSessionEntries(log, 5)
  // Turn 1 is fully known to the consumer → dropped. Turn 2 survives on its
  // closing frame alone (end_seq still names the row stamp). Turn 3 is open.
  assert.deepEqual(resync.turns.map((t) => t.end_seq), [6, undefined])
  assert.deepEqual(
    resync.turns[0].frames,
    full.turns[1].frames.filter((f) => (f as any).seq > 5),
  )
  assert.deepEqual(
    resync.turns[1].frames,
    full.turns[2].frames.filter((f) => (f as any).seq > 5),
  )
  // The log's high-water mark stays whole-log: it anchors the halt mint and
  // the consumer's "how far does this log reach", neither of which is a frame.
  assert.equal(resync.tail_seq, 8)
})

test('since_seq may sit between frames — a mint above it re-sends, at it does not', () => {
  // Mint seqs are fractional (the verdict-ask anchors at ask seq + 0.5), so
  // an integer-only boundary would force consumers to under-resync and
  // re-send frames they already hold — a duplicate Match in the assembler.
  const log = [
    ev(1, 'turn/start', { turn: 4 }),
    toolCall(2, 'c1', 'edit_document', {}),
    ev(3, 'approval/asked', { callId: 'c1', toolName: 'edit_document' }),
    toolResult(4, 'c1', 'applied'),
    ev(5, 'turn/end', { turn: 4, reason: { kind: 'completed' } }),
  ]
  const below = projectSessionEntries(log, 3)
  const at = projectSessionEntries(log, 3.5)
  assert.deepEqual(below.turns[0].frames.map((f) => (f as any).seq), [3.5, 4, 5])
  assert.deepEqual(at.turns[0].frames.map((f) => (f as any).seq), [4, 5])
})

test('the open turn survives since_seq even with zero new frames — the still-open signal', () => {
  // A gap with no frames after the consumer's last seq still must answer
  // "the turn is open", so the consumer does not halt what the driver still
  // holds. Only closed turns with no surviving frames drop.
  const log = [ev(1, 'turn/start', { turn: 1 }), assistantMessage(2, 'mid')]
  for (const since of [2, 99]) {
    const resync = projectSessionEntries(log, since)
    assert.equal(resync.turns.length, 1)
    assert.equal(resync.turns[0].end_seq, undefined)
    assert.deepEqual(resync.turns[0].frames, [])
  }
})

test('events before the first turn/start buffer into it, never dropped', () => {
  const replay = projectSessionEntries([
    ev(1, 'todo/write', { items: [] }),
    ev(2, 'turn/start', { turn: 1 }), assistantMessage(3, 'hi'), ev(4, 'turn/end', { turn: 1, reason: { kind: 'completed' } }),
  ])
  assert.equal(replay.turns.length, 1)
  const kinds = replay.turns[0].frames.map((f) => (f as any).kind ?? (f as any).type)
  assert.ok(kinds.includes('todo/write'), JSON.stringify(kinds))
})

test('the open turn carries the live-stream baseline — closed turns never do', () => {
  // A reload mid-step keeps the streamed text. The plugin folds every
  // relayed stream frame into dsh's
  // SessionAssistantStreamAccumulator (stream-baselines.ts); the projection
  // carries that fold's snapshot on the OPEN turn so the browser re-seats the
  // transient tail. A fold with NO active attempt (nothing streaming, or a
  // missed frame reset the fold) seats nothing and rides nothing — the reload
  // degrades to today's behaviour, never a wrong baseline.
  const baseline = {
    revision: 3,
    activeAttempt: {
      attemptId: 'a1', startedAfterSeq: 4, turn: 2, step: 0, nextIndex: 2,
      stream: [{ type: 'text-chunks', time0: 5, index: 0, dt: [1], texts: ['Hel', 'lo'] }],
    },
  }
  const log = [
    ev(1, 'turn/start', { turn: 1 }), assistantMessage(2, 'hi'), ev(3, 'turn/end', { turn: 1, reason: { kind: 'completed' } }),
    ev(4, 'turn/start', { turn: 2 }), assistantMessage(5, 'cut off'),
  ]
  const replay = projectSessionEntries(log, undefined, baseline as any)
  assert.equal(replay.turns[1].assistant_stream, baseline)
  assert.equal('assistant_stream' in replay.turns[0], false)
  // No fold passed (a session that never streamed): no field.
  assert.equal('assistant_stream' in projectSessionEntries(log).turns[1], false)
  // A revision-only fold (no active attempt): no field.
  assert.equal('assistant_stream' in projectSessionEntries(log, undefined, { revision: 4 }).turns[1], false)
})

test('a since_seq resync keeps the baseline on the open turn', () => {
  // The browser-WS-gap twin reads the same projection with since_seq — the
  // re-adopted turn still needs its streamed text.
  const baseline = {
    revision: 1,
    activeAttempt: {
      attemptId: 'a1', startedAfterSeq: 1, turn: 1, step: 0, nextIndex: 0, stream: [],
    },
  }
  const log = [ev(1, 'turn/start', { turn: 1 }), assistantMessage(2, 'mid')]
  const resync = projectSessionEntries(log, 2, baseline as any)
  assert.equal(resync.turns[0].assistant_stream, baseline)
})

test("a dead attempt's baseline never seats on a LATER open turn", () => {
  // A turn that died without a stream `end` (a driver crash) leaves its
  // attempt ACTIVE in the fold; a reload in the NEXT turn's start window
  // would serve the dead attempt's baseline onto the new open turn. The fold
  // attaches only when the attempt began inside the open turn:
  // startedAfterSeq at or above the open turn's own turn/start seq, recorded
  // in the same projection pass (one id space, one log — never a boundary
  // seq from outside it).
  const log = [
    ev(1, 'turn/start', { turn: 1 }), assistantMessage(2, 'hi'), ev(3, 'turn/end', { turn: 1, reason: { kind: 'completed' } }),
    ev(4, 'turn/start', { turn: 2 }), assistantMessage(5, 'cut off'),
  ]
  const dead = {
    revision: 3,
    activeAttempt: {
      attemptId: 'dead', startedAfterSeq: 1, turn: 1, step: 0, nextIndex: 1,
      stream: [{ type: 'text-chunks', time0: 2, index: 0, dt: [1], texts: ['old'] }],
    },
  }
  assert.equal('assistant_stream' in projectSessionEntries(log, undefined, dead as any).turns[1], false,
    'the attempt predates the open turn — its baseline is not served')
  const live = {
    revision: 4,
    activeAttempt: {
      attemptId: 'live', startedAfterSeq: 4, turn: 2, step: 0, nextIndex: 0, stream: [],
    },
  }
  assert.equal(projectSessionEntries(log, undefined, live as any).turns[1].assistant_stream, live,
    'an attempt that began at the open turn\'s start (or after) seats')
})

test('an empty log projects no turns and no tail', () => {
  assert.deepEqual(projectSessionEntries([]), { turns: [], tail_seq: null })
})

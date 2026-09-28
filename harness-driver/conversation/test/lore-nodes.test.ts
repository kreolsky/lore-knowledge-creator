/**
 * Lore's own nodes — the four cards dsh has no node for (plan
 * lore-renders-dsh-conversation step 2), fed as lore/* events into the SAME
 * assembler that renders dsh's vocabulary.
 *
 * What is asserted here is the step's contract: an out-of-band image outcome
 * lands at its anchor; an abnormal halt renders its card; and the lore nodes
 * interleave without moving a single dsh node — on the reload path exactly as
 * on the streamed one, INCLUDING when the live stream continues afterwards
 * (the case that made `splice` necessary, see the INVARIANT in src/index.ts).
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import test from 'node:test'
import assert from 'node:assert/strict'

import { createLoreConversation } from '../src/index.ts'
import { LORE_SEQ_OFFSETS, loreEvent, type LoreEventKind } from '../src/lore-events.ts'

const FIXTURE = join(import.meta.dirname, 'fixtures', 'session-turn.jsonl')

/** Read a v3 session JSONL (one event per row, no packed chunks) into assembler inputs. */
function loadFixture(): any[] {
  const inputs: any[] = []
  for (const line of readFileSync(FIXTURE, 'utf8').split('\n')) {
    if (line.trim() === '') continue
    const row = JSON.parse(line)
    if (row.seq === undefined) continue
    inputs.push({ type: 'event', event: row })
  }
  return inputs
}

/** A turn-less anchor: the last event logged BEFORE the first turn opens
 * (the session's own bookkeeping — no turn coordinate to inherit). */
function sessionLevelEvent(inputs: any[]): any {
  const events = inputs.map(input => input.event)
  const firstTurn = events.findIndex(event => event.type === 'turn/start')
  assert.ok(firstTurn > 0, 'the fixture has no event before its first turn/start')
  return events[firstTurn - 1]
}

function lastEvent(inputs: any[], type: string): any {
  const found = inputs.map(input => input.event).filter(event => event.type === type).at(-1)
  assert.ok(found, `the fixture has no ${type} event to anchor on`)
  return found
}

/** kind + id + data, for order- and content-sensitive comparison. */
function shape(nodes: readonly any[]): string[] {
  return nodes.map(node => `${node.kind}:${node.id}:${JSON.stringify(node.data)}`)
}

/** shape plus the resolved location — what an out-of-band feed can silently move. */
function placed(nodes: readonly any[]): string[] {
  return nodes.map(node => {
    const where = node.location.kind === 'turn' ? `turn:${node.location.turn.turn}` : node.location.kind
    return `${node.kind}:${node.id}:${where}:${JSON.stringify(node.data)}`
  })
}

const LORE_KINDS = ['image-gen', 'verdict-ask', 'halt', 'compaction-mint'] as const

/** One mint per fact; two facts of one kind never share an anchor (the seq
 * would collide — the placement is anchor + kind offset). */
function loreInputs(): any[] {
  const inputs = loadFixture()
  const calls = inputs.map(input => input.event).filter(event => event.type === 'tool/call')
  const end = lastEvent(inputs, 'turn/end')
  const seed = sessionLevelEvent(inputs)
  const time = end.time
  return [
    loreEvent('lore/verdict-ask', calls[calls.length - 1]!.seq, {
      turn: calls[calls.length - 1]!.data.turn,
      callId: 'call-edit',
      toolName: 'edit_document',
    }, time),
    loreEvent('lore/image-gen', calls[calls.length - 1]!.seq, {
      turn: calls[calls.length - 1]!.data.turn,
      runId: 'run-anchor',
      status: 'done',
      imageRefIds: ['ref-1', 'ref-2'],
      title: 'Мир документа',
      refine: { ok: true, prompt: 'refined prompt' },
    }, time),
    loreEvent('lore/halt', end.seq, {
      turn: end.data.turn,
      reason: 'output_token_limit',
      message: 'The model reached its output-token ceiling before finishing.',
    }, time),
    loreEvent('lore/compaction-mint', seed.seq, {
      turn: null,
      compactionEntryId: 'compaction-1',
      mintFailed: false,
      tokensBefore: 90000,
    }, time),
  ]
}

test('an image generation appended out of band lands at its anchor', () => {
  const inputs = loadFixture()
  const calls = inputs.map(input => input.event).filter(event => event.type === 'tool/call')
  const anchor = calls[calls.length - 1]!
  const earlier = calls[calls.length - 2]!

  const conversation = createLoreConversation()
  conversation.replaceWindow(inputs, false)
  conversation.splice(loreEvent('lore/image-gen', anchor.seq, {
    turn: anchor.data.turn,
    runId: 'run-anchor',
    status: 'done',
    imageRefIds: ['ref-1', 'ref-2'],
    title: 'Мир документа',
    refine: { ok: true, prompt: 'refined prompt' },
  }, anchor.time))
  const nodes = conversation.nodes()

  const anchorIndex = nodes.findIndex(node => node.kind === 'tool-call' && node.anchorSeq === anchor.seq)
  assert.ok(anchorIndex >= 0, 'the dispatching call has no tool row to anchor at')
  const gen = nodes[anchorIndex + 1]
  assert.equal(gen?.kind, 'image-gen', 'the image card does not sit immediately after its dispatching call')
  assert.equal(gen.id, 'run-anchor')
  assert.equal(gen.visibility, 'visible')
  assert.deepEqual(gen.data, {
    status: 'done',
    runId: 'run-anchor',
    imageRefIds: ['ref-1', 'ref-2'],
    title: 'Мир документа',
    refine: { ok: true, prompt: 'refined prompt' },
  })
  // The location is resolved from the mint's POSITION, not from the live
  // cursor: it sits inside the dispatching call's step of that turn.
  assert.equal(gen.location.kind, 'step')
  assert.equal(gen.location.turn.turn, anchor.data.turn)

  // A failed detached run renders its failure detail — never silence. Its
  // anchor is the call it dispatched from (a distinct one: placement is
  // anchor + kind offset, so two same-kind facts cannot share an anchor).
  conversation.splice(loreEvent('lore/image-gen', earlier.seq, {
    turn: earlier.data.turn,
    runId: 'run-failed',
    status: 'failed',
    error: 'comfy unreachable',
  }, earlier.time))
  const failed = conversation.nodes().find(node => node.id === 'run-failed')
  assert.equal(failed?.kind, 'image-gen')
  assert.equal(failed.data.status, 'failed')
  assert.equal(failed.data.error, 'comfy unreachable')
})

test('an abnormal halt renders its card', () => {
  const inputs = loadFixture()
  const end = lastEvent(inputs, 'turn/end')

  const conversation = createLoreConversation()
  conversation.replaceWindow(inputs, false)
  conversation.splice(loreEvent('lore/halt', end.seq, {
    turn: end.data.turn,
    reason: 'disconnected',
    message: 'The connection dropped mid-stream.',
    steps: 13,
  }, end.time))
  const nodes = conversation.nodes()

  const halt = nodes.find(node => node.kind === 'halt')
  assert.ok(halt, 'no halt card among the nodes')
  assert.deepEqual(halt.data, {
    reason: 'disconnected',
    message: 'The connection dropped mid-stream.',
    steps: 13,
  })
  assert.equal(halt.visibility, 'visible')
  assert.equal(halt.anchorSeq, end.seq + LORE_SEQ_OFFSETS.halt)
  assert.equal(halt.location.kind, 'turn')
  assert.equal(halt.location.turn.turn, end.data.turn)

  // A halt minted with no turn at all (no dsh anchor exists) still renders —
  // at session level, degraded but visible.
  const seed = sessionLevelEvent(inputs)
  conversation.splice(loreEvent('lore/halt', seed.seq, {
    turn: null,
    reason: 'deadline_breach',
  }, seed.time))
  const halts = conversation.nodes().filter(node => node.kind === 'halt')
  assert.equal(halts.length, 2, 'both halt cards render')
  const sessionHalt = halts.find(node => node.data.reason === 'deadline_breach')
  assert.ok(sessionHalt, 'the turn-less halt did not render')
  assert.equal(sessionHalt.location.kind, 'session')
})

test('the lore cards interleave without displacing a single dsh node', () => {
  const inputs = loadFixture()
  const lore = loreInputs()
  const callSeq = inputs.map(input => input.event).filter(event => event.type === 'tool/call').at(-1)!.seq

  const baseline = createLoreConversation()
  baseline.replaceWindow(inputs, false)
  const expected = shape(baseline.nodes())

  const conversation = createLoreConversation()
  conversation.replaceWindow(inputs, false)
  for (const input of lore) conversation.splice(input)
  const nodes = conversation.nodes()

  for (const kind of LORE_KINDS) {
    assert.ok(nodes.some(node => node.kind === kind), `no ${kind} node rendered`)
  }
  assert.deepEqual(
    shape(nodes.filter(node => !(LORE_KINDS as readonly string[]).includes(node.kind))),
    expected,
    'a dsh node moved when the lore cards landed',
  )

  // The verdict card sits immediately after the call it belongs to, before
  // the image card minted at the same anchor (offsets 0.5 < 0.6).
  const anchorIndex = nodes.findIndex(node => node.kind === 'tool-call' && node.anchorSeq === callSeq)
  assert.equal(nodes[anchorIndex + 1]?.kind, 'verdict-ask')
  assert.equal(nodes[anchorIndex + 2]?.kind, 'image-gen')

  // A reload equals the streamed tail — the constructor contract holds for
  // lore events spliced into the window.
  const reloaded = createLoreConversation()
  reloaded.replaceWindow([...inputs, ...lore], false)
  assert.deepEqual(shape(reloaded.nodes()), shape(nodes))
})

test('the live stream continues correctly after an out-of-band mint', () => {
  const inputs = loadFixture()
  const anchor = lastEvent(inputs, 'tool/call')
  const userMessage = inputs.map(input => input.event).find(event => event.type === 'user/message')
  assert.ok(userMessage, 'the fixture has no turn-less user message to stream after the mint')
  const tailSeq = Math.max(...inputs.map(input => input.event.seq))
  // The next live event carries NO turn of its own, so its location comes
  // from the index's cursor — the thing a behind-the-tail append would have
  // rewound to the mint's turn.
  const next = {
    event: {
      ...userMessage,
      seq: tailSeq + 1,
      data: { ...userMessage.data, id: 'msg-after-mint', messageId: 'msg-after-mint' },
    },
    type: 'event',
  } as any
  const mint = loreEvent('lore/image-gen', anchor.seq, {
    turn: anchor.data.turn,
    runId: 'run-detached',
    status: 'done',
    imageRefIds: ['ref-1'],
  }, anchor.time)

  const streamed = createLoreConversation()
  streamed.replaceWindow(inputs, false)
  streamed.splice(mint)
  streamed.append(next)

  const reloaded = createLoreConversation()
  reloaded.replaceWindow([...inputs, mint, next], false)

  assert.deepEqual(
    placed(streamed.nodes()),
    placed(reloaded.nodes()),
    'a mint spliced behind the tail moved a later event, so a reload disagrees with the stream',
  )
  const after = streamed.nodes().find(node => node.id === 'msg-after-mint')
  assert.ok(after, 'the event streamed after the mint rendered no node')
  assert.equal(after.location.kind, 'session', 'the mint rewound the location cursor')
})

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

test('an image generation appended out of band lands at its anchor', () => {  const inputs = loadFixture()
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

test('a run\'s live phases fold into ONE node ending done (the run rides the chat channel)', () => {
  // The run's phases are CHAT facts — RUNNING
  // lore/image-gen frames pushed per phase, then the settled frame at the
  // reload mint's exact position. The definition keys ONE context per run id
  // and its update takes the LATEST event, so the phases never render as
  // separate cards: running → done is one node whose data ends 'done'.
  const inputs = loadFixture()
  const anchor = lastEvent(inputs, 'tool/call')

  /** The k-th phase's WIRE position: anchor + 0.5 + 0.1·k/(k+1) — above a
   * verdict mint at the same anchor, below the settled +0.6, strictly
   * increasing so every phase APPENDS into the one context. */
  const phaseEvent = (k: number, phase: string) => ({
    type: 'event' as const,
    event: {
      type: 'lore/image-gen',
      seq: anchor.seq + 0.5 + 0.1 * k / (k + 1),
      time: anchor.time,
      data: { turn: anchor.data.turn, runId: 'run-live', status: 'running', phase },
      ignorable: true,
    },
  })

  const conversation = createLoreConversation()
  conversation.replaceWindow(inputs, false)
  conversation.splice(phaseEvent(1, 'refining'))
  conversation.splice(phaseEvent(2, 'queued'))
  conversation.splice(phaseEvent(3, 'generating'))
  conversation.splice(phaseEvent(4, 'downloading'))
  let gens = conversation.nodes().filter(node => node.kind === 'image-gen')
  assert.equal(gens.length, 1, 'the run\'s phases rendered as more than one node')
  assert.equal(gens[0].id, 'run-live')
  assert.deepEqual(gens[0].data, {
    runId: 'run-live', status: 'running', phase: 'downloading',
  })

  // The settled frame closes the run at +0.6 — the reload mint's position —
  // and the node's data becomes the settled fact.
  conversation.splice(loreEvent('lore/image-gen', anchor.seq, {
    turn: anchor.data.turn,
    runId: 'run-live',
    status: 'done',
    imageRefIds: ['ref-1'],
  }, anchor.time))
  gens = conversation.nodes().filter(node => node.kind === 'image-gen')
  assert.equal(gens.length, 1)
  assert.deepEqual(gens[0].data, {
    runId: 'run-live', status: 'done', imageRefIds: ['ref-1'],
  })
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

/** The fixture up to the second step's start — the step whose reply the model
 * was streaming when the stream failed. */
function failingStepInputs(): { inputs: any[]; stream: any[]; text: string; next: number; time: number } {
  const all = loadFixture()
  const reply = all.map(input => input.event)
    .filter(event => event.type === 'assistant/message').at(-1)
  assert.ok(reply, 'the fixture has no settled reply to cut off')
  const inputs = all.filter(input => input.event.seq < reply.seq)
  assert.equal(inputs.at(-1)!.event.type, 'step/start', 'the fixture reply does not open its own step')
  // A stream cut mid-flight: the deltas arrived, the block-ends and the finish did not.
  const stream = reply.data.stream.filter((record: any) =>
    record.type !== 'chunk' || record.chunk.type === 'block-start')
  const text = reply.data.message.content
    .filter((block: any) => block.type === 'text').map((block: any) => block.text).join('\n\n')
  return { inputs, stream, text, next: reply.seq, time: reply.time }
}

function dshEvent(type: string, seq: number, time: number, data: object, surfaceOp?: string): any {
  return { type: 'event', event: { type, seq, time, data, ...surfaceOp === undefined ? {} : { surfaceOp } } }
}

/** The error ending dsh logs once the failed attempt's retries are exhausted. */
function errorEnd(seq: number, time: number): any[] {
  return [
    dshEvent('step/end', seq, time, { turn: 1, step: 2 }),
    dshEvent('turn/end', seq + 1, time, { turn: 1, reason: { kind: 'error', error: { message: 'upstream 502' } } }),
  ]
}

function interrupted(nodes: readonly any[]): any[] {
  return nodes.filter(node => node.kind === 'interrupted-reply' && node.visibility === 'visible')
}

// TEST-WHY: recurring-regression — the reply text typed before an API error vanished from the chat, live and on reload.
test('a reply cut off by an API error stays at its step, above the error card', () => {
  const { inputs, stream, text, next, time } = failingStepInputs()
  const attempt = dshEvent('assistant/attempt', next, time, { turn: 1, step: 2, stream })
  const tail = [attempt, ...errorEnd(next + 1, time)]

  const reloaded = createLoreConversation()
  reloaded.replaceWindow([...inputs, ...tail], false)
  const nodes = reloaded.nodes()
  const cut = interrupted(nodes)
  assert.equal(cut.length, 1, 'the cut-off reply rendered no node (or more than one)')
  assert.equal(cut[0].data.text, text, 'the node does not carry the typed text without its reasoning')
  assert.equal(cut[0].anchorSeq, next, 'the node is not anchored at the failed attempt')
  assert.equal(cut[0].location.kind, 'step', 'the node is not located at its step')
  assert.equal(cut[0].location.step.step, 2, 'the node sits at the wrong step')
  const errorIndex = nodes.findIndex(node => node.kind === 'turn-error')
  assert.ok(errorIndex > nodes.indexOf(cut[0]), 'the error card does not sit below the cut-off reply')

  // The live stream assembles the same node in the same place as the reload.
  const streamed = createLoreConversation()
  streamed.replaceWindow(inputs, false)
  for (const input of tail) streamed.append(input)
  assert.deepEqual(placed(interrupted(streamed.nodes())), placed(cut))
})

// TEST-WHY: recurring-regression — the cut-off text vanished at the retry, so a Stop during the backoff left nothing after a reload.
test('every cut-off attempt stays through retries and the reply that finally settled', () => {
  const { inputs, stream, next, time } = failingStepInputs()
  const retry = (seq: number, n: number) => dshEvent('llm/retry', seq, time, {
    turn: 1, step: 2, retryId: 'retry-1', retry: n, maxRetries: 5, delayMs: 1000,
    error: { message: 'upstream 502' },
  })
  const reply = loadFixture().find(input => input.event.seq === next)!.event
  const tail = [
    dshEvent('assistant/attempt', next, time, { turn: 1, step: 2, stream }),
    retry(next + 1, 1),
    dshEvent('assistant/attempt', next + 2, time, { turn: 1, step: 2, stream }),
    retry(next + 3, 2),
    dshEvent('assistant/message', next + 4, time, reply.data, 'append'),
  ]
  // The live path reads the nodes after EVERY event, as the screen does.
  const live = createLoreConversation()
  live.replaceWindow(inputs, false)
  const counts = tail.map(input => { live.append(input); return interrupted(live.nodes()).length })
  assert.deepEqual(counts, [1, 1, 2, 2, 2], 'a cut-off attempt disappeared on the way')
  const nodes = live.nodes()
  const reloaded = createLoreConversation()
  reloaded.replaceWindow([...inputs, ...tail], false)
  assert.deepEqual(placed(interrupted(reloaded.nodes())), placed(interrupted(nodes)),
    'a reload does not show the same cut-off attempts')
  const settledIndex = nodes.findIndex(node => node.kind === 'assistant-step' && node.anchorSeq === next + 4)
  assert.ok(settledIndex > nodes.indexOf(interrupted(nodes).at(-1)), 'the settled reply is not below the cut-off attempts')
})

// TEST-WHY: recurring-regression — only an error-cut reply with text gets the node; a cancel already renders through dsh.
test('no cut-off node for a reasoning-only attempt or a cancelled reply', () => {
  const { inputs, stream, next, time } = failingStepInputs()
  const reasoningOnly = stream.filter((record: any) =>
    record.type === 'reasoning-chunks' || (record.type === 'chunk' && record.chunk.blockType === 'reasoning'))
  const thinking = createLoreConversation()
  thinking.replaceWindow([
    ...inputs, dshEvent('assistant/attempt', next, time, { turn: 1, step: 2, stream: reasoningOnly }),
    ...errorEnd(next + 1, time),
  ], false)
  assert.equal(interrupted(thinking.nodes()).length, 0, 'a reasoning-only attempt rendered a cut-off reply')

  const reply = loadFixture().find(input => input.event.seq === next)!.event
  const cancelled = createLoreConversation()
  cancelled.replaceWindow([
    ...inputs, dshEvent('assistant/message', next, time, { ...reply.data, stream, interrupted: true }, 'append'),
  ], false)
  const nodes = cancelled.nodes()
  assert.equal(interrupted(nodes).length, 0, 'a cancelled reply rendered a second, cut-off copy')
  assert.ok(nodes.some(node => node.kind === 'assistant-step' && node.visibility === 'visible'),
    'the cancelled reply itself did not render')
})

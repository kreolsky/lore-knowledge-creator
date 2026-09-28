/**
 * The engine Lore will render over, fed a REAL dsh session log.
 *
 * The turn in the fixture (a v3 log recorded on gray) thinks between tool
 * calls across six steps — the exact shape Lore's hand-written translator
 * flattened into one concatenated trace. What is asserted here is what step 3 will put on screen: reasoning
 * stays a separate block, in place, and a reload equals the live stream.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import test from 'node:test'
import assert from 'node:assert/strict'

import { createLoreConversation } from '../src/index.ts'

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

/** The kind+data pair each node carries, for order-sensitive comparison. */
function shape(nodes: readonly any[]): string[] {
  return nodes.map(node => `${node.kind}:${node.id}`)
}

test('a real session log assembles into ordered chat nodes', () => {
  const inputs = loadFixture()
  const types = inputs.map(input => input.event.type)
  assert.ok(types.includes('turn/start') && types.includes('turn/end'), 'the fixture is not a complete turn')

  const conversation = createLoreConversation()
  conversation.replaceWindow(inputs, false)
  const nodes = conversation.nodes()

  assert.ok(nodes.length > 0, 'the chat view produced no nodes')
  const kinds = new Set(nodes.map(node => node.kind))
  for (const expected of ['user', 'assistant-step', 'tool-call', 'turn-tail']) {
    assert.ok(kinds.has(expected), `no ${expected} node among ${[...kinds].join(', ')}`)
  }
})

test('a turn that thinks between tool calls keeps its reasoning in place', () => {
  const conversation = createLoreConversation()
  conversation.replaceWindow(loadFixture(), false)
  const nodes = conversation.nodes()

  const assistants = nodes.filter(node => node.kind === 'assistant-step')
  const steps = loadFixture().filter(input => input.event.type === 'step/start').length
  assert.ok(steps > 1, 'the fixture turn has a single step')
  assert.equal(assistants.length, steps, 'one assistant row per step of the fixture turn')

  // Every step of this turn opened with a reasoning block; none of them is
  // concatenated into the step's text, and each stays its own block.
  for (const assistant of assistants) {
    const blocks = (assistant.data as any).blocks as any[]
    assert.equal(blocks[0].kind, 'reasoning', `step ${(assistant.data as any).step} lost its reasoning block`)
    assert.ok(blocks[0].text.length > 0, 'the reasoning block is empty')
    const texts = blocks.filter(block => block.kind === 'text')
    for (const text of texts) {
      assert.ok(!text.text.includes(blocks[0].text), 'reasoning was folded into the text block')
    }
  }

  // In place: the reasoning of a step is rendered BEFORE that step's tools,
  // not hoisted to the head of the turn.
  const firstTool = nodes.findIndex(node => node.kind === 'tool-call')
  const laterAssistant = nodes.findIndex((node, index) => node.kind === 'assistant-step' && index > firstTool)
  assert.ok(firstTool >= 0 && laterAssistant > firstTool, 'no assistant row follows a tool row')
})

test('a replayed window equals the streamed tail', () => {
  const inputs = loadFixture()

  const reloaded = createLoreConversation()
  reloaded.replaceWindow(inputs, false)

  const streamed = createLoreConversation()
  streamed.replaceWindow([], false)
  for (const input of inputs) streamed.append(input)

  assert.deepEqual(shape(streamed.nodes()), shape(reloaded.nodes()))
  assert.deepEqual(
    streamed.nodes().map(node => JSON.stringify(node.data)),
    reloaded.nodes().map(node => JSON.stringify(node.data)),
  )
})

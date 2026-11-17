/**
 * Tests for the persona's preload/memory context messages (preload.ts). Pure
 * module, run at image build with the other plugin suites: the dedupe against
 * the active surface is per skill NAME, so a persona switched mid-chat gets
 * its own preloads injected while an already-injected one is not repeated.
 * The live wire (the pre-step listener, the dsh log) is the drive's to prove.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type { Message } from '@deepseek-ai/dsh-llm'

import { memoryAction, MEMORY_OFF_NOTE, preloadMessages } from '../src/preload.ts'
import type { ResolvedSkill } from '../src/skills.ts'

function skill(name: string): ResolvedSkill {
  return { name, description: `d ${name}`, tools: [], location: `doc-${name}`, body: `body of ${name}`, child_docs: [] }
}

function injectedPreload(names: string[]): Message {
  return createUserMessage({
    content: [{ type: 'text', text: names.join(',') }],
    source: { kind: 'lore-preload', names },
  })
}

// TEST-WHY: pure-logic-edge — a persona switched mid-chat (A preloaded, B now) must get B's skills injected, and A's skill already on the surface must not be injected twice.
test('a surface preload message suppresses only the names it carries', () => {
  const history = [injectedPreload(['web-search'])]
  const out = preloadMessages([skill('web-search'), skill('chat-recall')], 1, () => history)
  assert.equal(out.length, 1, 'one preload message for the missing skill')
  assert.deepEqual((out[0].source as { names: string[] }).names, ['chat-recall'])
  const text = out[0].content.map((b) => (b.type === 'text' ? b.text : '')).join('')
  assert.ok(text.includes('body of chat-recall'))
  assert.ok(!text.includes('body of web-search'), 'the already-injected skill is not repeated')

  const again = preloadMessages([skill('web-search'), skill('chat-recall')], 1,
    () => [...history, ...out])
  assert.deepEqual(again, [], 'every resolved name is on the surface ⇒ nothing injected')
})

const text = (m: Message) => m.content.map((b) => (b.type === 'text' ? b.text : '')).join('')

// TEST-WHY: pure-logic-edge — a persona switch must replace the old persona's memory node (block or off-note), while the same persona or an untagged pre-change node keeps it.
test('the memory block is swapped only on a persona change', () => {
  assert.equal(memoryAction('A mem', 'a', 2, () => []), null, 'only the first step acts')
  assert.equal(memoryAction('', 'a', 1, () => []), null, 'no block, no node ⇒ nothing')
  const first = memoryAction('A mem', 'a', 1, () => [])
  assert.equal(first?.kind, 'append')
  assert.equal(memoryAction('A mem', 'a', 1, () => [{ seq: 5, persona: 'a' }]), null, 'same persona')
  assert.equal(memoryAction('B mem', 'b', 1, () => [{ seq: 5, persona: undefined }]), null, 'untagged node')

  const toB = memoryAction('B mem', 'b', 1, () => [{ seq: 5, persona: 'a' }])
  assert.equal(toB?.kind, 'replace')
  assert.ok(toB?.kind === 'replace' && toB.seq === 5 && toB.from === 'a' && text(toB.message) === 'B mem')
  assert.equal((toB!.message.source as { persona: string }).persona, 'b')

  const off = memoryAction('', '', 1, () => [{ seq: 5, persona: 'a' }])
  assert.ok(off?.kind === 'replace' && text(off.message) === MEMORY_OFF_NOTE)
})

/**
 * Tests for the persona's preload/memory/recall context messages (preload.ts).
 * Pure module, run at image build with the other plugin suites: the dedupe
 * against the active surface is per skill NAME, so a persona switched mid-chat
 * gets its own preloads injected while an already-injected one is not repeated;
 * a block node is swapped only on a persona change, and an off note never
 * blocks a later block of its own persona. The live wire (the pre-step
 * listener, the dsh log) is the drive's to prove.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type { Message } from '@deepseek-ai/dsh-llm'

import {
  MEMORY_KIND, MEMORY_OFF_NOTE, RECALL_KIND, RECALL_OFF_NOTE,
  memoryAction, preloadMessages, resolvePreloads, surfaceMemoryNodes,
} from '../src/preload.ts'
import type { MemoryNode } from '../src/preload.ts'
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

// TEST-WHY: pure-logic-edge — a preload entry resolves as a name or a mask, a skill two entries name is kept once, and an entry the persona's own allow/deny cancels is logged with the reason instead of a bare unknown.
test('preload entries resolve as names or masks, once each; a hidden one is skipped with its reason', () => {
  const original = console.error
  const lines: string[] = []
  console.error = (line: unknown) => { lines.push(String(line)) }
  try {
    const visible = [skill('main-read'), skill('main-edit'), skill('web-search')]
    const hidden = [
      { skill: skill('main-files'), reason: 'deny main-files' },
      { skill: skill('tables'), reason: 'outside allow' },
      { skill: skill('main-config'), reason: 'deny main-*' },
    ]
    const out = resolvePreloads(
      ['main-*', 'main-read', 'web-search', 'tables', 'main-files', 'ghost'], visible, hidden)
    assert.deepEqual(out.map((s) => s.name), ['main-read', 'main-edit', 'web-search'],
      'a mask expands in catalog order and a skill two entries name is kept once')
    assert.deepEqual(lines, [
      '[lore-skills] preload skipped tables: tables hidden (outside allow)',
      '[lore-skills] preload skipped main-files: main-files hidden (deny main-files)',
      '[lore-skills] preload unknown ghost',
    ])
    // The same lists with nothing hidden say only what is unknown.
    lines.length = 0
    assert.deepEqual(resolvePreloads(['main-*'], visible).map((s) => s.name), ['main-read', 'main-edit'])
    assert.deepEqual(lines, [])
  } finally {
    console.error = original
  }
})

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
  const none = () => [] as MemoryNode[]
  assert.equal(memoryAction('A mem', 'a', 2, none, MEMORY_KIND, MEMORY_OFF_NOTE), null, 'only the first step acts')
  assert.equal(memoryAction('', 'a', 1, none, MEMORY_KIND, MEMORY_OFF_NOTE), null, 'no block, no node ⇒ nothing')
  const first = memoryAction('A mem', 'a', 1, none, MEMORY_KIND, MEMORY_OFF_NOTE)
  assert.equal(first?.kind, 'append')
  assert.equal(memoryAction('A mem', 'a', 1, () => [{ seq: 5, persona: 'a', off: false }], MEMORY_KIND, MEMORY_OFF_NOTE), null, 'same persona')
  assert.equal(memoryAction('B mem', 'b', 1, () => [{ seq: 5, persona: undefined, off: false }], MEMORY_KIND, MEMORY_OFF_NOTE), null, 'untagged node')

  const toB = memoryAction('B mem', 'b', 1, () => [{ seq: 5, persona: 'a', off: false }], MEMORY_KIND, MEMORY_OFF_NOTE)
  assert.equal(toB?.kind, 'replace')
  assert.ok(toB?.kind === 'replace' && toB.seq === 5 && toB.from === 'a' && text(toB.message) === 'B mem')
  assert.equal((toB!.message.source as { persona: string; off?: boolean }).persona, 'b')
  assert.equal((toB!.message.source as { off?: boolean }).off, undefined, 'a real block is not an off note')

  const off = memoryAction('', '', 1, () => [{ seq: 5, persona: 'a', off: false }], MEMORY_KIND, MEMORY_OFF_NOTE)
  assert.ok(off?.kind === 'replace' && text(off.message) === MEMORY_OFF_NOTE)
  assert.equal((off!.message.source as { off?: boolean }).off, true, 'the off note is marked as one')
})

// TEST-WHY: pure-logic-edge — a persona switch replaces the recalled node with the new persona's block or the recall's own off note, and a block sent after that note for the same persona still lands — the server records it as delivered.
test('the recall node is swapped on a persona change, and an off note never blocks a later block', () => {
  const a = { seq: 3, persona: 'a', off: false }
  const offNote = { seq: 9, persona: 'b', off: true }
  const of = (...nodes: MemoryNode[]) => () => nodes

  assert.equal(memoryAction('', 'a', 1, of(), RECALL_KIND, RECALL_OFF_NOTE), null, 'no block, no node ⇒ nothing (no off note from nothing)')
  const first = memoryAction('A recall', 'a', 1, of(), RECALL_KIND, RECALL_OFF_NOTE)
  assert.equal(first?.kind, 'append')
  assert.equal((first!.message.source as { kind: string }).kind, RECALL_KIND, 'under its own kind')
  assert.equal(memoryAction('A recall', 'a', 2, of(), RECALL_KIND, RECALL_OFF_NOTE), null, 'only the first step acts')
  assert.equal(memoryAction('A recall', 'a', 1, of(a), RECALL_KIND, RECALL_OFF_NOTE), null, 'same persona, a real block')

  const off = memoryAction('', 'b', 1, of(a), RECALL_KIND, RECALL_OFF_NOTE)
  assert.ok(off?.kind === 'replace' && off.seq === 3 && off.from === 'a' && text(off.message) === RECALL_OFF_NOTE)
  assert.ok(text(off!.message) !== MEMORY_OFF_NOTE, 'the note is the recall\'s own, not the memory\'s')
  const toA = memoryAction('A again', 'a', 1, of(offNote), RECALL_KIND, RECALL_OFF_NOTE)
  assert.ok(toA?.kind === 'replace' && text(toA.message) === 'A again', 'another persona replaces the off note')

  const later = memoryAction('B recall', 'b', 1, of(offNote), RECALL_KIND, RECALL_OFF_NOTE)
  assert.ok(later?.kind === 'replace' && later.seq === 9 && later.from === 'b' && text(later.message) === 'B recall',
    'a block replaces the SAME persona\'s off note')
  assert.equal((later!.message.source as { off?: boolean }).off, undefined)
  assert.equal(memoryAction('', 'b', 1, of(offNote), RECALL_KIND, RECALL_OFF_NOTE), null, 'no block ⇒ the off note stays')
  assert.equal(memoryAction('B recall', 'b', 1, of({ seq: 9, persona: 'b', off: false }), RECALL_KIND, RECALL_OFF_NOTE), null,
    'a real block of the same persona stays')
  assert.equal(memoryAction('B recall', 'b', 1, of({ seq: 9, persona: undefined, off: false }), RECALL_KIND, RECALL_OFF_NOTE), null,
    'untagged node')
})

// TEST-WHY: pure-logic-edge — the surface scan returns each kind's own nodes, with the persona tag and the off-note mark read off the log.
test('surfaceMemoryNodes reads only its kind off the active surface', () => {
  const user = (source: unknown) => ({
    type: 'user/message',
    data: createUserMessage({ content: [{ type: 'text', text: 'x' }], source: source as never }),
  })
  const session = {
    surface: { nodes: [1, 2, 3, 4, 5] },
    eventAt(seq: number) {
      return [
        user({ kind: MEMORY_KIND, persona: 'a' }),
        user({ kind: RECALL_KIND, persona: 'b', off: true }),
        user({ kind: 'lore-preload', names: ['x'] }),
        { type: 'turn/start', data: {} },
        user({ kind: 'user' }),
      ][seq - 1]
    },
  }
  assert.deepEqual(surfaceMemoryNodes(session, MEMORY_KIND), [{ seq: 1, persona: 'a', off: false }])
  assert.deepEqual(surfaceMemoryNodes(session, RECALL_KIND), [{ seq: 2, persona: 'b', off: true }])
})

/**
 * Tests for the turn's time ground as a context message (plan
 * chat-time-stamps-as-context-message). Pure module, run at image build with
 * the other plugin suites: the parse degrades garbage payloads, and the
 * message is the stamps' OWN `lore-time` user message — one text block, step 1
 * only. The live wire (the pre-step listener's position in the waterfall, the
 * titler never seeing the bytes) is the drive's to prove.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { parseTimeStamps, timeStampMessage } from '../src/time-stamps.ts'

// ── timeStampMessage ──────────────────────────────────────────────────────────

test('step 1: one user message, source kind lore-time, text = lines joined with blank line', () => {
  const msg = timeStampMessage(['[a]', '[b]'], 1)
  assert.ok(msg, 'step 1 with stamps must produce a message')
  assert.equal(msg.role, 'user')
  assert.equal((msg.source as { kind: string }).kind, 'lore-time')
  assert.deepEqual(
    msg.content,
    [{ type: 'text', text: '[a]\n\n[b]' }],
    'ONE text block of the lines joined with \\n\\n',
  )
})

test('later steps return undefined — the ground must not repeat on a tool loop', () => {
  assert.equal(timeStampMessage(['[a]'], 2), undefined)
  assert.equal(timeStampMessage(['[a]'], 0), undefined)
})

test('no stamps returns undefined', () => {
  assert.equal(timeStampMessage([], 1), undefined)
})

// ── parseTimeStamps ───────────────────────────────────────────────────────────

test('keeps string items in order', () => {
  assert.deepEqual(parseTimeStamps(['[a]', '[b]']), ['[a]', '[b]'])
})

test('undefined / non-array / non-string items degrade to [] or are dropped', () => {
  assert.deepEqual(parseTimeStamps(undefined), [])
  assert.deepEqual(parseTimeStamps('nope'), [])
  assert.deepEqual(parseTimeStamps({}), [])
  assert.deepEqual(parseTimeStamps([1, 'a', null, 'b', ['c']]), ['a', 'b'])
})

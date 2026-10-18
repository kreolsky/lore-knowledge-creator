/**
 * The session agent against a REAL dsh AgentLoop (JSONL persistence, a gated
 * script adapter): the inbox is the queue, claims swap the prep, the frames
 * the backend mints rows from, the gated idle dispose and the creation-time
 * clear of a resumed or forked log.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { Context } from '@deepseek-ai/cordis'
import LlmRuntime, { LlmAdapter, createUserMessage } from '@deepseek-ai/dsh-llm'
import type { GenerateOptions, LlmResolvedModelInfo, StreamChunk } from '@deepseek-ai/dsh-llm'
import SessionStore, { SessionId } from '@deepseek-ai/dsh-session'
import SessionProjectionRegistry from '@deepseek-ai/dsh-session-projection'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import ToolRuntime from '@deepseek-ai/dsh-tools'
import AgentRegistry from '@deepseek-ai/dsh-agent'
import JsonlSessionPersistence from '@deepseek-ai/dsh-session-persistence-jsonl'
import AgentLoop from '@deepseek-ai/dsh-agent-loop'

import { createSessionEventTap } from '../src/ws-events.ts'
import { loreTurnCtxFor } from '../src/tools.ts'

process.env.LORE_DRIVER_SECRET ||= 't3st-s3cr3t'
const { resumeOrCreate, sessionFork } = await import('../src/sessions.ts')
const {
  disposeSessionAgents, discardQueued, enqueueFollowup, sessionAgents, sessionStatus, stopSession,
} = await import('../src/session-agents.ts')
type TurnPrep = import('../src/session-agents.ts').TurnPrep

// ── a gated script adapter: each model call takes the next entry; a gate
// holds the call open until the test releases it (or the turn aborts).

interface Entry { text?: string; gate?: Promise<void>; fail?: string }

function textChunks(text: string): StreamChunk[] {
  return [
    { type: 'block-start', index: 0, blockType: 'text' },
    { type: 'text-delta', index: 0, text },
    { type: 'block-end', index: 0, block: { type: 'text', text } },
    { type: 'usage', usage: { inputTokens: 10, outputTokens: 1 } },
    { type: 'finish', reason: { kind: 'stop' } },
  ]
}

class ScriptAdapter extends LlmAdapter {
  requests: GenerateOptions[] = []
  constructor(private script: Entry[]) { super() }
  override resolveModel(provider: string, model: string): Promise<LlmResolvedModelInfo> {
    return Promise.resolve({ provider, id: model, name: model })
  }
  async * stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
    this.requests.push(options)
    const entry = this.script.shift()
    if (!entry) throw new Error('ScriptAdapter: script exhausted')
    if (entry.gate) {
      await Promise.race([entry.gate, new Promise<void>((_ok, fail) => {
        if (options.signal?.aborted) fail(new Error('aborted'))
        options.signal?.addEventListener('abort', () => fail(new Error('aborted')), { once: true })
      })])
    }
    if (entry.fail) throw new Error(entry.fail)
    for (const chunk of textChunks(entry.text ?? 'ok')) yield chunk
  }
}

function gate(): { promise: Promise<void>; open(): void } {
  const g = Promise.withResolvers<void>()
  return { promise: g.promise, open: () => g.resolve() }
}

// ── harness

const roots: string[] = []
const contexts: Context[] = []

async function harness(script: Entry[], idleMs = 60_000) {
  const root = await mkdtemp(join(tmpdir(), 'lore-agents-'))
  roots.push(root)
  const ctx = new Context()
  contexts.push(ctx)
  await ctx.plugin(LlmRuntime)
  await ctx.plugin(SessionStore)
  await ctx.plugin(SessionProjectionRegistry)
  await ctx.plugin(SystemPrompt)
  await ctx.plugin(ToolRuntime)
  await ctx.plugin(AgentRegistry)
  await ctx.plugin(JsonlSessionPersistence, { root })
  await ctx.plugin(AgentLoop, { agents: [] })
  const adapter = new ScriptAdapter(script)
  ctx.llm.registerAdapter(['mock'], adapter)
  const tap = createSessionEventTap()
  ctx.on('session/event', (session: any, ev: any) => tap.emit(session, ev))
  const frames: Array<{ id: string; frame: any }> = []
  const opened: string[] = []
  const deps = {
    ctx: ctx as any,
    channel: { push: (id: string, frame: any) => { frames.push({ id, frame }) }, close() {} },
    tap,
    open: (id: string, options: Record<string, unknown>, setup: (c: any) => void) => {
      opened.push(id)
      return resumeOrCreate(ctx as any, id, options, setup)
    },
    idleMs,
    contextUsage: () => null,
  }
  return { ctx, adapter, deps, frames, opened, tap }
}

test.afterEach(async () => {
  disposeSessionAgents()
  // The context owns the agents: disposing it drains their writers before
  // the log directory goes.
  for (const ctx of contexts.splice(0)) await (ctx as any).fiber.dispose()
  for (const root of roots.splice(0)) await rm(root, { recursive: true, force: true })
})

function prep(text: string, over: Partial<TurnPrep> = {}): TurnPrep {
  return {
    loreId: 'lore-x', model: 'm1', cap: 0,
    selection: { provider: 'mock', model: 'm1' },
    systemPrompt: 'SYSTEM one',
    timeStamps: [],
    servedNames: new Set(), skills: [], activated: new Set(),
    standingCtx: {
      loreSessionId: 'lore-x', agentKey: 'k', mutating: new Set(), holdable: new Set(),
      regionTools: new Set(),
    },
    requestCtx: { messageId: `a-${text}`, applyMode: 'confirm', document_id: null, region: null },
    claim: {
      user_msg_id: `u-${text}`, assistant_msg_id: `a-${text}`, user_text: text,
      images: [], files: [], sources: [], warnings: [],
    },
    ...over,
  }
}

const OPTS = { provider: 'mock', model: 'm1' }
const FILE = { id: 'f1', name: 'a.zip', size: 3, mime: 'application/zip', path: '/workspace/x/inbox/chat/f1/a.zip' }

function send(h: Awaited<ReturnType<typeof harness>>, id: string, text: string, p = prep(text)) {
  return enqueueFollowup(h.deps as any, id, OPTS, p,
    async () => [{ type: 'text', text }])
}

function textOf(message: any): string {
  const content = message?.content
  if (typeof content === 'string') return content
  return (Array.isArray(content) ? content : [])
    .filter((b: any) => b?.type === 'text').map((b: any) => b.text).join('')
}

/** The user-role texts a request carries, in order. */
function userTexts(req: GenerateOptions): string[] {
  return req.messages.filter((m: any) => m.role === 'user').map(textOf)
}

/** The texts of the user's own messages (source kind 'user') a request carries. */
function sentTexts(req: GenerateOptions): string[] {
  return req.messages
    .filter((m: any) => m.role === 'user' && m.source?.kind === 'user').map(textOf)
}

async function until(pred: () => boolean, what: string, ms = 5000): Promise<void> {
  const start = Date.now()
  while (!pred()) {
    if (Date.now() - start > ms) throw new Error(`timed out waiting for ${what}`)
    await new Promise((r) => setTimeout(r, 5))
  }
}

const settled = (id: string) => () => {
  const s = sessionStatus(id)
  return s.status === 'idle' && s.queue.length === 0
}

function framesOf(h: { frames: Array<{ id: string; frame: any }> }, type: string): any[] {
  return h.frames.filter((f) => f.frame.type === type).map((f) => f.frame)
}

// ── the queue

test('a send during a running turn queues in the inbox — accepted, never refused', async () => {
  const g = gate()
  const h = await harness([{ text: 'A1', gate: g.promise }, { text: 'B1' }])
  const first = await send(h, 's-queue', 'A')
  await until(() => h.adapter.requests.length === 1, 'the first model call')
  const second = await send(h, 's-queue', 'B')
  assert.notEqual(first, second, 'each send answers its own dsh message id')
  const status = sessionStatus('s-queue')
  assert.equal(status.status, 'running')
  assert.deepEqual(status.queue.map((m: any) => m.id), [second],
    'the queued send is the inbox next-turn list, its dsh id the snapshot item')
  assert.equal(typeof status.seq, 'number')
  g.open()
  await until(settled('s-queue'), 'the queue to drain')
})

test('two queued sends run as two turns in send order, one claim each', async () => {
  const g = gate()
  const h = await harness([{ text: 'A1', gate: g.promise }, { text: 'B1' }, { text: 'C1' }])
  await send(h, 's-two', 'A')
  await until(() => h.adapter.requests.length === 1, 'the first model call')
  await send(h, 's-two', 'B')
  await send(h, 's-two', 'C')
  g.open()
  await until(settled('s-two'), 'the queue to drain')
  await until(() => framesOf(h, 'turn_closed').length === 3, 'three terminals')
  assert.deepEqual(h.adapter.requests.map((r) => sentTexts(r).at(-1)), ['A', 'B', 'C'])
  assert.deepEqual(framesOf(h, 'lore/claim').map((f) => f.user_text), ['A', 'B', 'C'])
  // Wire order: each turn's terminal lands before the next turn's claim.
  const order = h.frames.map((f) => f.frame.type)
    .filter((t) => t === 'lore/claim' || t === 'turn_closed')
  assert.deepEqual(order,
    ['lore/claim', 'turn_closed', 'lore/claim', 'turn_closed', 'lore/claim', 'turn_closed'])
})

test('the claim frame carries the prep facts the backend mints the pair from', async () => {
  const h = await harness([{ text: 'R' }])
  const facts = {
    user_msg_id: 'u-1', assistant_msg_id: 'a-1', user_text: 'hello',
    images: ['data:image/png;base64,AAAA'], files: [FILE], sources: [{ id: 'doc' }],
    warnings: [{ code: 'image_not_delivered' }],
  }
  const messageId = await send(h, 's-claim', 'hello', prep('hello', { model: 'm-claim', claim: facts }))
  await until(settled('s-claim'), 'the turn')
  const [claim] = framesOf(h, 'lore/claim')
  assert.deepEqual(claim, { type: 'lore/claim', message_id: messageId, ...facts, model: 'm-claim' })
  const types = h.frames.map((f) => f.frame.type)
  assert.ok(types.indexOf('lore/claim') < types.indexOf('model_update'),
    'the claim opens the turn; model_update follows it')
  assert.deepEqual(framesOf(h, 'dsh_status').map((f) => f.status), ['running', 'idle'],
    'every idle⇄running transition relays as dsh_status')
})

test('✕ removes a queued message: no turn runs for it; ✕ after its claim answers false', async () => {
  const g = gate()
  const h = await harness([{ text: 'A1', gate: g.promise }, { text: 'C1' }])
  const a = await send(h, 's-discard', 'A')
  await until(() => h.adapter.requests.length === 1, 'the first model call')
  const b = await send(h, 's-discard', 'B')
  await send(h, 's-discard', 'C')
  assert.equal(await discardQueued('s-discard', b), true)
  assert.equal(await discardQueued('s-discard', a), false, 'a claimed message is no longer pending')
  assert.equal(await discardQueued('s-nobody', b), false, 'no live agent → nothing to remove')
  g.open()
  await until(settled('s-discard'), 'the queue to drain')
  assert.deepEqual(h.adapter.requests.map((r) => sentTexts(r).at(-1)), ['A', 'C'])
  assert.deepEqual(framesOf(h, 'lore/claim').map((f) => f.user_text), ['A', 'C'])
})

test('an errored turn HOLDS the queue; the next send runs all of it in send order, its own last', async () => {
  const g = gate()
  const h = await harness([
    { gate: g.promise, fail: 'model exploded' }, { text: 'B1' }, { text: 'C1' }, { text: 'D1' },
  ])
  await send(h, 's-hold', 'A')
  await until(() => h.adapter.requests.length === 1, 'the first model call')
  await send(h, 's-hold', 'B')
  await send(h, 's-hold', 'C')
  g.open()
  await until(() => sessionStatus('s-hold').status === 'idle', 'the errored turn to end')
  await new Promise((r) => setTimeout(r, 50))
  assert.equal(h.adapter.requests.length, 1, 'nothing after the error starts on its own')
  assert.equal(sessionStatus('s-hold').queue.length, 2, 'the queue is held')
  await send(h, 's-hold', 'D')
  await until(settled('s-hold'), 'the held queue to drain')
  assert.deepEqual(h.adapter.requests.map((r) => sentTexts(r).at(-1)), ['A', 'B', 'C', 'D'])
})

test("the person's Stop takes the queue out and returns its text, images and files, in send order", async () => {
  const h = await harness([{ gate: new Promise(() => {}) }, { text: 'D1' }])
  await send(h, 's-back', 'A')
  await until(() => h.adapter.requests.length === 1, 'the first model call')
  await send(h, 's-back', 'B', prep('B', { claim: {
    user_msg_id: 'u-B', assistant_msg_id: 'a-B', user_text: 'B',
    images: ['data:image/png;base64,AAA'], files: [FILE], sources: [], warnings: [],
  } }))
  await send(h, 's-back', 'C')
  const reply = stopSession('s-back', true)
  assert.equal(reply.stopped, true)
  assert.deepEqual(reply.returned, [
    { text: 'B', images: ['data:image/png;base64,AAA'], files: [FILE] },
    { text: 'C', images: [], files: [] },
  ])
  assert.equal(sessionStatus('s-back').queue.length, 0, 'the inbox is empty at once')
  await until(() => sessionStatus('s-back').status === 'idle', 'the cancelled turn to end')
  await new Promise((r) => setTimeout(r, 50))
  assert.equal(h.adapter.requests.length, 1, 'nothing queued runs after Stop')
  assert.deepEqual(stopSession('s-back', true), { stopped: false, returned: [] })
  await send(h, 's-back', 'D')
  await until(settled('s-back'), 'the next send')
  assert.deepEqual(h.adapter.requests.map((r) => sentTexts(r).at(-1)), ['A', 'D'],
    'the returned messages never run')
})

test('a stop that parks (the backend deadline) keeps the queue; the next send starts it', async () => {
  const h = await harness([{ gate: new Promise(() => {}) }, { text: 'B1' }, { text: 'C1' }])
  await send(h, 's-stop', 'A')
  await until(() => h.adapter.requests.length === 1, 'the first model call')
  await send(h, 's-stop', 'B')
  assert.deepEqual(stopSession('s-stop'), { stopped: true, returned: [] })
  await until(() => sessionStatus('s-stop').status === 'idle', 'the cancelled turn to end')
  await new Promise((r) => setTimeout(r, 50))
  assert.equal(sessionStatus('s-stop').queue.length, 1, 'the queue survives Stop')
  assert.equal(h.adapter.requests.length, 1, 'no auto-continue after Stop')
  assert.equal(stopSession('s-stop').stopped, false, 'nothing running → stopped:false')
  await send(h, 's-stop', 'C')
  await until(settled('s-stop'), 'the parked queue to drain')
  assert.deepEqual(h.adapter.requests.map((r) => sentTexts(r).at(-1)), ['A', 'B', 'C'])
})

// ── one agent per session

test('two concurrent first sends share ONE agent creation', async () => {
  const h = await harness([{ text: 'A1' }, { text: 'B1' }])
  await Promise.all([send(h, 's-race', 'A'), send(h, 's-race', 'B')])
  await until(settled('s-race'), 'both turns')
  assert.deepEqual(h.opened, ['s-race'], 'one open (resume-or-create) for the session')
  assert.equal(h.adapter.requests.length, 2, 'both sends ran')
})

test('the idle dispose waits out a send in flight, then releases the agent', async () => {
  const h = await harness([{ text: 'A1' }, { text: 'B1' }], 20)
  await send(h, 's-idle', 'A')
  await until(settled('s-idle'), 'the first turn')
  const agent = sessionAgents.get('s-idle')
  assert.ok(agent, 'the agent stays live between turns')
  // A send whose content build is still running when the idle timer fires.
  const contentGate = gate()
  const pending = enqueueFollowup(h.deps as any, 's-idle', OPTS, prep('B'), async () => {
    await contentGate.promise
    return [{ type: 'text', text: 'B' }]
  })
  await new Promise((r) => setTimeout(r, 80))
  assert.equal(sessionAgents.get('s-idle'), agent, 'a send in flight blocks the dispose')
  contentGate.open()
  await pending
  await until(settled('s-idle'), 'the second turn')
  assert.deepEqual(h.opened, ['s-idle'], 'the second send ran on the SAME agent')
  assert.deepEqual(h.adapter.requests.map((r) => sentTexts(r).at(-1)), ['A', 'B'])
  await until(() => !sessionAgents.has('s-idle'), 'the idle dispose')
})

test('each claim swaps in ITS prep: model, prompt, time ground, request ctx', async () => {
  const g = gate()
  const h = await harness([{ text: 'A1', gate: g.promise }, { text: 'B1' }])
  let regionAtSecondCall: unknown = 'unset'
  const origStream = h.adapter.stream.bind(h.adapter)
  h.adapter.stream = (options: GenerateOptions) => {
    if (h.adapter.requests.length === 1) regionAtSecondCall = loreTurnCtxFor('s-prep')?.region
    return origStream(options)
  }
  await send(h, 's-prep', 'A', prep('A', {
    model: 'm1', selection: { provider: 'mock', model: 'm1' }, systemPrompt: 'SYSTEM one',
    timeStamps: ['[stamp one]'],
  }))
  await until(() => h.adapter.requests.length === 1, 'the first model call')
  await send(h, 's-prep', 'B', prep('B', {
    model: 'm2', selection: { provider: 'mock', model: 'm2' }, systemPrompt: 'SYSTEM two',
    timeStamps: ['[stamp two]'],
    requestCtx: { messageId: 'a-B', applyMode: 'auto', document_id: 'doc-2', region: { start: 7 } },
  }))
  g.open()
  await until(settled('s-prep'), 'both turns')
  const [first, second] = h.adapter.requests
  assert.equal(first.model, 'm1')
  assert.equal(second.model, 'm2', 'the second step runs the second prep\'s model')
  const system = (req: GenerateOptions) => textOf(req.messages.find((m: any) => m.role === 'system'))
  assert.match(system(first), /SYSTEM one/)
  assert.match(system(second), /SYSTEM two/, 'the prompt section reads the current prep')
  assert.ok(userTexts(second).includes('[stamp two]'), 'the second turn carries its own time ground')
  assert.ok(!userTexts(second).slice(userTexts(second).indexOf('B')).includes('[stamp one]'),
    'the first prep\'s stamp is not repeated on the second turn')
  assert.deepEqual(regionAtSecondCall, { start: 7 },
    'the tool ctx request half is the second prep\'s while its step runs')
})

// ── the creation-time clear

async function storedEvents(ctx: any, id: string): Promise<any[]> {
  const handle = await ctx.sessionPersistence.open(SessionId(id), 'read')
  try {
    return [...(await handle.read()).events]
  } finally {
    await handle.close()
  }
}

test('a resumed log ending in an unclaimed insert starts EMPTY: the next send runs alone', async () => {
  const h = await harness([{ text: 'A1' }, { text: 'F1' }])
  await send(h, 's-src', 'A')
  await until(settled('s-src'), 'the source turn')
  await h.ctx.sessions.flush(sessionAgents.get('s-src')!.agent.session)
  const events = await storedEvents(h.ctx, 's-src')
  // The insert that queued "A" is a real splice row; a copy with a fresh
  // message, appended at the next seq, is a send queued before a restart.
  const insert = events.find((e) => e.type === 'agent/inbox/spliced' && e.data.inserted.length === 1)
  const stale = createUserMessage({ content: [{ type: 'text', text: 'stale' }], source: { kind: 'user' } })
  const seeded = [...events, {
    ...insert, seq: events.length,
    data: { target: 'next-turn', start: 0, inserted: [stale] },
  }]
  const detached = (h.ctx as any).sessions.prepare(SessionId('s-restarted'))
  const writer = await (h.ctx as any).sessionPersistence.create(detached.header)
  await writer.append(seeded)
  await writer.close()

  await send(h, 's-restarted', 'fresh')
  await until(settled('s-restarted'), 'the fresh turn')
  assert.equal(h.adapter.requests.length, 2, 'one model call for the restarted session')
  assert.deepEqual(sentTexts(h.adapter.requests[1]), ['A', 'fresh'],
    'the pre-restart message never runs')
  assert.deepEqual(framesOf(h, 'lore/claim').map((f) => f.user_text), ['A', 'fresh'])
})

test('a branch forked after a mid-turn insert starts with an EMPTY inbox', async () => {
  const g = gate()
  const h = await harness([{ text: 'A1', gate: g.promise }, { text: 'B1' }, { text: 'X1' }])
  await send(h, 's-trunk', 'A')
  await until(() => h.adapter.requests.length === 1, 'the first model call')
  await send(h, 's-trunk', 'B') // inserted mid-turn: before turn 1's turn/end
  g.open()
  await until(settled('s-trunk'), 'the trunk turns')
  await h.ctx.sessions.flush(sessionAgents.get('s-trunk')!.agent.session)
  const events = await storedEvents(h.ctx, 's-trunk')
  const boundary = events.find((e) => e.type === 'turn/end')!.seq
  const insertB = events.findIndex((e) => e.type === 'agent/inbox/spliced'
    && e.data.inserted.some((m: any) => textOf(m) === 'B'))
  assert.ok(insertB >= 0 && insertB < boundary, 'B\'s insert sits before the fork boundary')

  const res: any = {
    status: 0, body: '',
    writeHead(code: number) { res.status = code; return res },
    end(chunk?: string) { res.body = chunk ?? ''; return res },
  }
  const req = new EventEmitter() as any
  req.headers = { 'x-driver-secret': process.env.LORE_DRIVER_SECRET }
  req.setEncoding = () => {}
  queueMicrotask(() => {
    req.emit('data', JSON.stringify({ source_dsh_id: 's-trunk', seq: boundary, new_lore_id: 's-branch' }))
    req.emit('end')
  })
  await sessionFork(h.ctx as any, { get: (id: string) => id } as any, req, res)
  assert.equal(res.status, 200, res.body)

  await send(h, 's-branch', 'X')
  await until(settled('s-branch'), 'the branch turn')
  assert.equal(h.adapter.requests.length, 3, 'one model call for the branch')
  assert.deepEqual(sentTexts(h.adapter.requests[2]), ['A', 'X'],
    'the trunk\'s queued B never runs on the branch')
})

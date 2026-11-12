/**
 * The personal-MCP spike, promoted to the step's gate (plan
 * personal-mcp-servers step 1): a REAL dsh AgentLoop plus a REAL
 * streamable-http MCP fixture proves the riskiest facts —
 * a plugin mounts from the claim into a LIVE agent scope; the tools reach
 * the FIRST request of the mounting turn (the bounded assembly wait); two
 * agents with different headers keep different connections; the read-only
 * mark runs without an ask and an unmarked write asks through the existing
 * verdict bridge (allow_session stops the next ask); the scrub replaces a
 * server echoing `Authorization` in results AND error results; a stalled
 * server costs at most the mount bound; a cross-origin redirect drops
 * `Authorization`; a second agent's scope sees none of the mounts.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { createServer as createNetServer } from 'node:net'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { z } from 'zod'
import { McpServer, createMcpHandler, type CallToolResult } from '@modelcontextprotocol/server'
import { toNodeHandler, type NodeIncomingMessageLike } from '@modelcontextprotocol/node'

import { Context } from '@deepseek-ai/cordis'
import LlmRuntime, { LlmAdapter } from '@deepseek-ai/dsh-llm'
import type { GenerateOptions, LlmResolvedModelInfo, StreamChunk } from '@deepseek-ai/dsh-llm'
import SessionStore, { SessionId } from '@deepseek-ai/dsh-session'
import SessionProjectionRegistry from '@deepseek-ai/dsh-session-projection'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import ToolRuntime from '@deepseek-ai/dsh-tools'
import AgentRegistry from '@deepseek-ai/dsh-agent'
import AgentLoop from '@deepseek-ai/dsh-agent-loop'
import UserApproval from '@deepseek-ai/dsh-user-approval'
import JsonlSessionPersistence from '@deepseek-ai/dsh-session-persistence-jsonl'

import { createSessionEventTap } from '../src/ws-events.ts'
import { loreTurnCtxFor } from '../src/tools.ts'
import { registerLoreApprovalBridge, resolveVerdict, resetLoreApprovalBridgeForTest } from '../src/approvals.ts'
import { MCP_MOUNT_WAIT_MS } from '../src/mcp-servers.ts'

process.env.LORE_DRIVER_SECRET ||= 't3st-s3cr3t'
const { resumeOrCreate } = await import('../src/sessions.ts')
const {
  disposeSessionAgents, enqueueFollowup, sessionAgents, sessionStatus,
} = await import('../src/session-agents.ts')
type TurnPrep = import('../src/session-agents.ts').TurnPrep

// ── the MCP fixture: one server, per-token views.

interface FixtureTools {
  whoami: boolean
  echo_auth: boolean
  write_thing: boolean
  boom_auth: boolean
}

/** The running fixture: its URL, every Authorization header it saw (in
 * order), and the write_thing call count. */
interface McpFixture {
  url: string
  authorizations: Array<string | undefined>
  writes: number
  close: () => Promise<void>
}

async function startMcpFixture(tools: Partial<FixtureTools> = {}): Promise<McpFixture> {
  const authorizations: Array<string | undefined> = []
  let writes = 0
  const handler = createMcpHandler(() => {
    const mcp = new McpServer(
      { name: 'spike-fixture', version: '1.0.0' },
      { capabilities: { tools: {} } },
    )
    if (tools.whoami !== false) {
      mcp.registerTool('whoami', {
        description: 'Hashes the Authorization header.',
        inputSchema: z.object({}),
        annotations: { readOnlyHint: true },
      }, async (): Promise<CallToolResult> => ({
        content: [{ type: 'text', text: hashOf(authorizations.at(-1)) }],
      }))
    }
    if (tools.echo_auth !== false) {
      mcp.registerTool('echo_auth', {
        description: 'Echoes the raw Authorization header.',
        inputSchema: z.object({}),
        annotations: { readOnlyHint: true },
      }, async (): Promise<CallToolResult> => ({
        content: [{ type: 'text', text: String(authorizations.at(-1)) }],
      }))
    }
    if (tools.write_thing !== false) {
      mcp.registerTool('write_thing', {
        description: 'Writes a thing (no read-only mark).',
        inputSchema: z.object({ thing: z.string() }),
      }, async (): Promise<CallToolResult> => {
        writes += 1
        return { content: [{ type: 'text', text: 'written' }] }
      })
    }
    if (tools.boom_auth !== false) {
      mcp.registerTool('boom_auth', {
        description: 'An error result echoing the header.',
        inputSchema: z.object({}),
        annotations: { readOnlyHint: true },
      }, async (): Promise<CallToolResult> => ({
        content: [{ type: 'text', text: `auth was ${String(authorizations.at(-1))}` }],
        isError: true,
      }))
    }
    return mcp
  })
  const handle = toNodeHandler(handler)
  const server = createServer((request: IncomingMessage, response: ServerResponse) => {
    authorizations.push(request.headers.authorization)
    handle(request as NodeIncomingMessageLike, response).catch((error: unknown) => {
      response.writeHead(500).end(String(error))
    })
  })
  const listening: PromiseWithResolvers<void> = Promise.withResolvers()
  server.listen(0, '127.0.0.1', listening.resolve)
  await listening.promise
  const address = server.address()
  if (address === null || typeof address === 'string') throw new Error('fixture has no TCP address')
  return {
    url: `http://127.0.0.1:${address.port}/mcp`,
    authorizations,
    get writes() { return writes },
    close: async () => {
      await handler.close()
      await new Promise<void>((resolve, reject) => {
        server.close((error) => { if (error === undefined) resolve(); else reject(error) })
      })
    },
  }
}

function hashOf(value: string | undefined): string {
  return createHash('sha256').update(String(value)).digest('hex').slice(0, 16)
}

// ── a script adapter whose entries can request tool calls.

interface Entry { text?: string; fail?: string; toolCall?: { id: string; name: string; args: string } }

function textChunks(text: string): StreamChunk[] {
  return [
    { type: 'block-start', index: 0, blockType: 'text' },
    { type: 'text-delta', index: 0, text },
    { type: 'block-end', index: 0, block: { type: 'text', text } },
    { type: 'usage', usage: { inputTokens: 10, outputTokens: 1 } },
    { type: 'finish', reason: { kind: 'stop' } },
  ]
}

function toolCallChunks(call: { id: string; name: string; args: string }): StreamChunk[] {
  const block = { type: 'tool-call' as const, id: call.id, name: call.name, arguments: call.args }
  return [
    { type: 'block-start', index: 0, blockType: 'tool-call' },
    { type: 'block-end', index: 0, block },
    { type: 'usage', usage: { inputTokens: 10, outputTokens: 1 } },
    { type: 'finish', reason: { kind: 'tool-calls' } },
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
    if (entry.fail) throw new Error(entry.fail)
    for (const chunk of entry.toolCall ? toolCallChunks(entry.toolCall) : textChunks(entry.text ?? 'ok')) {
      yield chunk
    }
  }
}

// ── harness

const roots: string[] = []
const contexts: Context[] = []
const sessionEvents: Array<{ id: string; ev: any }> = []

async function harness(script: Entry[], idleMs = 60_000) {
  const root = await mkdtemp(join(tmpdir(), 'lore-mcp-'))
  roots.push(root)
  const ctx = new Context()
  contexts.push(ctx)
  await ctx.plugin(LlmRuntime)
  await ctx.plugin(SessionStore)
  await ctx.plugin(SessionProjectionRegistry)
  await ctx.plugin(SystemPrompt)
  await ctx.plugin(ToolRuntime)
  await ctx.plugin(UserApproval, { policy: 'ask' })
  await ctx.plugin(AgentRegistry)
  await ctx.plugin(JsonlSessionPersistence, { root })
  await ctx.plugin(AgentLoop, { agents: [] })
  const adapter = new ScriptAdapter(script)
  ctx.llm.registerAdapter(['mock'], adapter)
  const tap = createSessionEventTap()
  ctx.on('session/event', (session: any, ev: any) => {
    tap.emit(session, ev)
    sessionEvents.push({ id: String(session?.id ?? ''), ev })
  })
  const frames: Array<{ id: string; frame: any }> = []
  const deps = {
    ctx: ctx as any,
    channel: { push: (id: string, frame: any) => { frames.push({ id, frame }) }, close() {} },
    tap,
    open: (id: string, options: Record<string, unknown>, setup: (c: any) => void) =>
      resumeOrCreate(ctx as any, id, options, setup),
    idleMs,
    contextUsage: () => null,
  }
  registerLoreApprovalBridge(ctx, loreTurnCtxFor)
  return { ctx, adapter, deps, frames }
}

test.afterEach(async () => {
  disposeSessionAgents()
  resetLoreApprovalBridgeForTest()
  sessionEvents.length = 0
  for (const ctx of contexts.splice(0)) await (ctx as any).fiber.dispose()
  for (const root of roots.splice(0)) await rm(root, { recursive: true, force: true })
})

function prep(text: string, over: Partial<TurnPrep> = {}): TurnPrep {
  return {
    loreId: 'lore-x', model: 'm1', cap: 0,
    selection: { provider: 'mock', model: 'm1' },
    systemPrompt: 'SYSTEM one',
    timeStamps: [], servedNames: new Set(), skills: [], activated: new Set(),
    mcpServers: [],
    standingCtx: {
      loreSessionId: 'lore-x', agentKey: 'k', mutating: new Set(), holdable: new Set(),
      perCallApproval: new Set(),
    },
    requestCtx: { messageId: `a-${text}`, applyMode: 'confirm', document_id: null, vision: false },
    claim: {
      user_msg_id: `u-${text}`, assistant_msg_id: `a-${text}`, user_text: text,
      images: [], files: [], sources: [], warnings: [],
    },
    ...over,
  }
}

const OPTS = { provider: 'mock', model: 'm1' }

function mcpServer(fixtureUrl: string, token: string): import('../src/mcp-servers.ts').McpServerWire {
  return {
    name: 'spike', url: fixtureUrl, title: 'Spike fixture',
    headers: { Authorization: `Bearer ${token}` },
    secret_values: [token],
  }
}

function send(h: Awaited<ReturnType<typeof harness>>, id: string, text: string, p = prep(text)) {
  return enqueueFollowup(h.deps as any, id, OPTS, p,
    async () => [{ type: 'text', text }])
}

async function until(pred: () => boolean, what: string, ms = 8000): Promise<void> {
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

function textOf(message: any): string {
  const content = message?.content
  if (typeof content === 'string') return content
  return (Array.isArray(content) ? content : [])
    .filter((b: any) => b?.type === 'text').map((b: any) => b.text).join('')
}

/** The full text of every message a request carries (the scrub surface). */
function allText(req: GenerateOptions): string {
  return req.messages.map(textOf).join('\n')
}

function toolNames(req: GenerateOptions): Set<string> {
  return new Set((req.tools ?? []).map((t: any) => t.function?.name ?? t.name))
}

async function toolResultText(dshId: string, name: string): Promise<string> {
  const events = sessionEvents.filter((e) => e.id === dshId)
  const call = [...events].reverse()
    .find((e) => e.ev?.type === 'tool/call' && e.ev?.data?.name === name)
  assert.ok(call, `a tool/call for ${name}`)
  // The result cites its call's seq through sourceEventSeqs.
  const result = events.find((e) => e.ev?.type === 'tool/result'
    && Array.isArray(e.ev?.sourceEventSeqs) && e.ev.sourceEventSeqs[0] === call.ev.seq)
  assert.ok(result, `a tool/result citing ${name}'s call`)
  return (result.ev.data?.message?.content ?? []).map((b: any) => b.text ?? '').join('')
}

// ── the spike facts.

test('a turn mounting a server reaches its tools in the FIRST request and runs one', async () => {
  const fixture = await startMcpFixture()
  try {
    const token = 'tok-AAA'
    const h = await harness([
      { toolCall: { id: 'c1', name: 'mcp__spike__whoami', args: '{}' } },
      { text: 'done' },
    ])
    await send(h, 's-mount', 'look in spike', prep('look in spike', {
      mcpServers: [mcpServer(fixture.url, token)],
    }))
    await until(settled('s-mount'), 'the turn')
    const [first] = h.adapter.requests
    assert.ok(first, 'one request ran')
    assert.ok(toolNames(first).has('mcp__spike__whoami'),
      'the FIRST request of the mounting turn already lists the tool (the bounded assembly wait)')
    assert.equal(allText(first).includes(token), false, 'the token never rides a request')
    const result = await toolResultText('s-mount', 'mcp__spike__whoami')
    assert.equal(result, hashOf(`Bearer ${token}`), 'the call ran with THIS user\'s header')
    assert.ok(!result.includes(token))
  } finally {
    await fixture.close()
  }
})

// TEST-WHY: cross-process-contract — a real MCP fixture's tool named like a native tool reaches the FIRST model request with the external prefix and the twin sentence, the native tool's description unchanged
test('an external twin of a native tool is described as external and names the native one', async () => {
  const fixture = await startMcpFixture({ whoami: true, echo_auth: false, write_thing: true, boom_auth: false })
  try {
    const h = await harness([{ text: 'done' }])
    // The host's own tool, registered the way the Lore proxies are (root ctx).
    h.ctx.tools.register({
      name: 'whoami',
      description: 'NATIVE whoami.',
      parameters: { type: 'object', properties: {} },
      output: { schema: { type: 'string' }, render: (_a: unknown, v: unknown) => [{ type: 'text', text: String(v) }] },
      async execute(): Promise<string> { return 'native' },
    } as any)
    await send(h, 's-twin', 'who', prep('who', {
      mcpServers: [mcpServer(fixture.url, 'tok-TWIN')],
    }))
    await until(settled('s-twin'), 'the turn')
    const [first] = h.adapter.requests
    assert.ok(first, 'one request ran')
    const described = new Map((first.tools ?? []).map((t: any) => [t.function?.name ?? t.name, String(t.function?.description ?? t.description)]))
    const external = '[External MCP service "Spike fixture" (server spike) — acts on that service, not on this project.] '
    assert.equal(described.get('mcp__spike__whoami'),
      external + "Twin of this project's own `whoami`: the same operation, run on the external service. For this project call `whoami`. "
      + 'Hashes the Authorization header.',
      'the twin carries the external prefix AND the sentence naming the native tool')
    assert.equal(described.get('mcp__spike__write_thing'), external + 'Writes a thing (no read-only mark).',
      'a non-twin carries only the external prefix')
    assert.equal(described.get('whoami'), 'NATIVE whoami.', 'the native tool is untouched')
  } finally {
    await fixture.close()
  }
})

test('two agents with different headers keep different connections', async () => {
  const fixture = await startMcpFixture()
  try {
    const [red, black] = ['tok-RED', 'tok-BLACK']
    const h = await harness([
      { toolCall: { id: 'c1', name: 'mcp__spike__whoami', args: '{}' } },
      { text: 'ok' },
      { toolCall: { id: 'c2', name: 'mcp__spike__whoami', args: '{}' } },
      { text: 'ok' },
    ])
    await send(h, 's-red', 'r', prep('r', {
      loreId: 'lore-red', standingCtx: {
        loreSessionId: 'lore-red', agentKey: 'k', mutating: new Set(), holdable: new Set(),
        perCallApproval: new Set(),
      },
      mcpServers: [mcpServer(fixture.url, red)],
    }))
    await until(settled('s-red'), 'red\'s turn')
    await send(h, 's-black', 'b', prep('b', {
      loreId: 'lore-black', standingCtx: {
        loreSessionId: 'lore-black', agentKey: 'k', mutating: new Set(), holdable: new Set(),
        perCallApproval: new Set(),
      },
      mcpServers: [mcpServer(fixture.url, black)],
    }))
    await until(settled('s-black'), 'black\'s turn')
    assert.equal(await toolResultText('s-red', 'mcp__spike__whoami'), hashOf(`Bearer ${red}`))
    assert.equal(await toolResultText('s-black', 'mcp__spike__whoami'), hashOf(`Bearer ${black}`),
      'each session\'s transport carried its own token (namespaces isolated per agent scope)')
    assert.ok(fixture.authorizations.includes(`Bearer ${red}`))
    assert.ok(fixture.authorizations.includes(`Bearer ${black}`))
  } finally {
    await fixture.close()
  }
})

test('a marked read-only tool runs with no ask; an unmarked write asks and allow_session grants', async () => {
  const fixture = await startMcpFixture()
  try {
    const token = 'tok-ASK'
    const h = await harness([
      { toolCall: { id: 'c1', name: 'mcp__spike__whoami', args: '{}' } },
      { toolCall: { id: 'c2', name: 'mcp__spike__write_thing', args: '{"thing":"x"}' } },
      { text: 'wrote' },
      { toolCall: { id: 'c3', name: 'mcp__spike__write_thing', args: '{"thing":"y"}' } },
      { text: 'wrote again' },
    ])
    // Turn one: whoami (marked read-only) never asks; write_thing parks on
    // the approval/asked audit event — the event the verdict card mints from.
    const first = send(h, 's-ask', 'write', prep('write', {
      mcpServers: [mcpServer(fixture.url, token)],
    }))
    await until(() => sessionEvents.some((e) => e.id === 's-ask'
      && e.ev?.type === 'approval/asked'
      && e.ev?.data?.toolName === 'mcp__spike__write_thing'), 'the write\'s approval/asked')
    const asked = sessionEvents.find((e) => e.id === 's-ask'
      && e.ev?.type === 'approval/asked'
      && e.ev?.data?.toolName === 'mcp__spike__write_thing')!.ev
    assert.equal(sessionEvents.filter((e) => e.id === 's-ask'
      && e.ev?.type === 'approval/asked').length, 1,
      'exactly one ask so far — whoami never asked (the read-only mark)')
    const status = resolveVerdict(String(asked.data.callId), 'lore-x', 'allow_session',
      'mcp__spike__write_thing', null)
    assert.equal(status, 200, 'the verdict resolves the parked ask')
    await first
    await until(settled('s-ask'), 'the granted turn')
    assert.equal(fixture.writes, 1, 'the first write ran after the grant')
    // Turn two: the grant covers the same tool in the same chat — no ask.
    await send(h, 's-ask', 'write again', prep('write again', {
      mcpServers: [mcpServer(fixture.url, token)],
    }))
    await until(settled('s-ask'), 'the second turn')
    assert.equal(fixture.writes, 2, 'both write calls ran')
    assert.equal(sessionEvents.filter((e) => e.id === 's-ask'
      && e.ev?.type === 'approval/asked').length, 1,
      'the second write never asked again (allow_session grant)')
  } finally {
    await fixture.close()
  }
})

test('the scrub replaces a secret echoed in results and error results alike', async () => {
  const fixture = await startMcpFixture()
  try {
    const token = 'tok-SCRUB'
    const h = await harness([
      { toolCall: { id: 'c1', name: 'mcp__spike__echo_auth', args: '{}' } },
      { toolCall: { id: 'c2', name: 'mcp__spike__boom_auth', args: '{}' } },
      { text: 'seen both' },
    ])
    await send(h, 's-scrub', 'echo', prep('echo', {
      mcpServers: [mcpServer(fixture.url, token)],
    }))
    await until(settled('s-scrub'), 'the turn')
    assert.equal(await toolResultText('s-scrub', 'mcp__spike__echo_auth'), 'Bearer [secret]',
      'a success result echoing the header keeps only the non-secret words')
    assert.ok((await toolResultText('s-scrub', 'mcp__spike__boom_auth')).includes('[secret]'),
      'an ERROR result echoing the header is scrubbed too')
    for (const [index, req] of h.adapter.requests.entries()) {
      assert.equal(allText(req).includes(token), false,
        'the model never sees the token (value and error content both)')
      if (index > 0) {
        assert.ok(allText(req).includes('[secret]'),
          'from the second request on, the marker reaches the model instead')
      }
    }
  } finally {
    await fixture.close()
  }
})

test('a stalled server costs at most the mount bound and its tools are absent', async () => {
  const stall = createNetServer((socket) => { socket.resume() })
  const listening: PromiseWithResolvers<void> = Promise.withResolvers()
  stall.listen(0, '127.0.0.1', listening.resolve)
  await listening.promise
  const port = (stall.address() as { port: number }).port
  try {
    const h = await harness([{ text: 'no tools then' }])
    const started = Date.now()
    await send(h, 's-stall', 'hello', prep('hello', {
      mcpServers: [{ name: 'stall', url: `http://127.0.0.1:${port}/mcp`, title: 'stall',
        headers: { Authorization: 'Bearer tok-stall' }, secret_values: ['tok-stall'] }],
    }))
    await until(settled('s-stall'), 'the turn', MCP_MOUNT_WAIT_MS + 5000)
    assert.ok(Date.now() - started < MCP_MOUNT_WAIT_MS + 4000,
      'the turn ended within the bound (no 60 s SDK hang)')
    const [first] = h.adapter.requests
    assert.ok(first)
    assert.equal([...toolNames(first)].filter((n) => n.startsWith('mcp__')).length, 0,
      'the stalled server contributed no tools')
  } finally {
    stall.close()
  }
})

test('a cross-origin redirect drops Authorization on the way', async () => {
  const fixture = await startMcpFixture({ whoami: true, echo_auth: false, write_thing: false, boom_auth: false })
  try {
    // The redirector: same host, ANOTHER port — a different origin for fetch,
    // which must strip Authorization before following.
    const redirects: Array<string | undefined> = []
    const redirector = createServer((req: IncomingMessage, res: ServerResponse) => {
      redirects.push(req.headers.authorization)
      res.writeHead(302, { location: fixture.url }).end()
    })
    const listening: PromiseWithResolvers<void> = Promise.withResolvers()
    redirector.listen(0, '127.0.0.1', listening.resolve)
    await listening.promise
    const port = (redirector.address() as { port: number }).port
    try {
      const token = 'tok-REDIR'
      // Text-only script: whether the transport survives the redirect or not,
      // the FACT under test is what each hop's server actually saw.
      const h = await harness([{ text: 'done' }])
      await send(h, 's-redir', 'via redirect', prep('via redirect', {
        mcpServers: [{ name: 'spike', url: `http://127.0.0.1:${port}/mcp`, title: 'spike',
          headers: { Authorization: `Bearer ${token}` }, secret_values: [token] }],
      }))
      await until(settled('s-redir'), 'the turn')
      assert.ok(redirects.includes(`Bearer ${token}`), 'the first hop carried the header')
      assert.ok(fixture.authorizations.every((a) => a === undefined),
        'the redirect target received NO Authorization (fetch strips it cross-origin)')
    } finally {
      await new Promise<void>((resolve) => redirector.close(() => resolve()))
    }
  } finally {
    await fixture.close()
  }
})

test('a second agent scope sees none of another agent\'s mounts', async () => {
  const fixture = await startMcpFixture({ whoami: true, echo_auth: false, write_thing: false, boom_auth: false })
  try {
    const h = await harness([{ text: 'one' }, { text: 'two' }])
    await send(h, 's-owner', 'mine', prep('mine', {
      mcpServers: [mcpServer(fixture.url, 'tok-owner')],
    }))
    await until(settled('s-owner'), 'the owner turn')
    const owner = sessionAgents.get('s-owner')
    assert.ok(owner?.agentCtx, 'the owner agent exists')
    const otherHandle = await (h.ctx as any).agents.create({
      sessionId: SessionId('s-other'), meta: { cwd: process.cwd() }, agentOptions: OPTS,
    })
    try {
      const ownerView = new Set((owner.agentCtx as any).tools.schemas(owner.agent).map((t: any) => t.name))
      const otherView = new Set((otherHandle.agent.ctx as any).tools.schemas(otherHandle.agent).map((t: any) => t.name))
      assert.ok(ownerView.has('mcp__spike__whoami'), 'the mounting agent sees its tools')
      assert.equal([...otherView].some((n) => n.startsWith('mcp__')), false,
        'an independent agent scope (the subagent shape: preset-joined, not scope-joined) sees none')
    } finally {
      await otherHandle.dispose()
    }
  } finally {
    await fixture.close()
  }
})

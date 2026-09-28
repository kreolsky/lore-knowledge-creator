/**
 * Tests for the Lore driver plugin (run at image build:
 * `node --import tsx/esm --test`, mirroring the retired line-A driver's build-time tests).
 * No harness boot — the fork machinery and index.ts are the units under test
 * here; the dsh→frame relay contract lives in map.test.ts.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { lastTurnEndSeq, turnEndIndex } from '../src/leaf.ts'
import { createSessionEventTap, type EventsChannel } from '../src/ws-events.ts'
import { newTurnMapState } from '../src/map.ts'
import { READ_TOOL_TIMEOUT_MS, registerLoreTools } from '../src/tools.ts'
import {
  clearSessionToolCtx, clearTurnRequestCtx, loreTurnCtxFor,
  setSessionToolCtx, stampTurnRequestCtx,
} from '../src/tools.ts'
import { installModelSelection } from '@deepseek-ai/dsh-agent'
import type { ModelSelectionRef } from '@deepseek-ai/dsh-agent'
import { ReasoningEffortId } from '@deepseek-ai/dsh-llm'

// index.ts captures the driver secret at MODULE LOAD (const SECRET), so the
// leaf-handler drive below needs it set BEFORE that module evaluates — which
// a static import cannot do (imports hoist above any env assignment). The
// dynamic import runs after both env guards; every other test file keeps
// plain imports because only index.ts reads this secret.
process.env.LORE_DRIVER_SECRET ||= 't3st-s3cr3t'
process.env.LORE_HARNESS_MODEL ||= 'test-model'
const {
  contextUsageFrame, harnessModel, PROVIDER, sessionLeaf,
} = await import('../src/index.ts')

function ev(seq: number, type: string, data?: any, surfaceOp?: any): any {
  return { seq, type, data, surfaceOp }
}

// Real 0.1.5-rc.2 shapes: surfaceOp is the STRING 'append' or the object
// {op:'replace',start,end}; user/message carries the payload as `data` itself.
function userMsg(seq: number, text: string): any {
  return ev(seq, 'user/message',
    { role: 'user', content: [{ type: 'text', text }] }, 'append')
}

function assistantMsg(seq: number, text: string, model: string): any {
  return ev(seq, 'assistant/message', {
    message: {
      role: 'assistant', model,
      source: { kind: 'model', provider: 'lore', model },
      content: [{ type: 'text', text }],
    },
  }, 'append')
}

function log(): any[] {
  return [
    ev(0, 'session/title', { title: 't' }), // log-only noise
    ev(1, 'turn/start', { turn: 1 }),
    userMsg(2, 'Q1'),
    ev(3, 'assistant/chunk', { chunk: { type: 'text-delta', text: 'A1' } }),
    assistantMsg(4, 'A1', 'm1'),
    ev(5, 'turn/end', { reason: { kind: 'completed' } }),
    ev(6, 'turn/start', { turn: 2 }),
    userMsg(7, 'Q2'),
    assistantMsg(8, 'A2', 'm2'),
    ev(9, 'turn/end', { reason: { kind: 'completed' } }),
  ]
}

// ─── leaf: the dsh seq IS the branch-point id ────────────────────────────────
// (plan collapse-the-editor-harness-layer step 5: the ordinal translation —
// the Nth turn/end — is deleted; the backend names a DRIVER id directly.)

test('a seq resolves only to ITS turn/end row; anything else is unresolvable', () => {
  const events = log()
  assert.equal(turnEndIndex(events, 5), 5)   // branch after turn 1 (log index of seq 5's row)
  assert.equal(turnEndIndex(events, 9), 9)   // branch after turn 2
  // A seq that exists but is NOT a turn/end is not a forkable boundary.
  assert.equal(turnEndIndex(events, 4), -1)
  assert.equal(turnEndIndex(events, 2), -1)
  // Unknown / out-of-range / malformed seqs never resolve.
  assert.equal(turnEndIndex(events, 99), -1)
  assert.equal(turnEndIndex([], 5), -1)
  assert.equal(turnEndIndex(events, 2.5), -1)
  assert.equal(turnEndIndex(events, -1), -1)
})

test('a log-only tail after the last turn never moves the live tail', () => {
  const events = log()
  events.push(ev(10, 'session/end-seed', {}))
  assert.equal(lastTurnEndSeq(events), 9)
})

test('the live tail is the LAST turn/end seq — the no-op comparison is seq equality', () => {
  const events = log()
  // Seam A names the parent turn's own seq on EVERY linear turn; equality
  // with the last turn/end is what keeps the leaf a no-op there (an earlier
  // boundary seq is a real fork). A log with no completed turn has none.
  assert.equal(lastTurnEndSeq(events), 9)
  assert.equal(lastTurnEndSeq([]), -1)
  assert.equal(lastTurnEndSeq([ev(0, 'session/title', {})]), -1)
})

// ─── persona carry: ONE prompt source (plan part B step 4) ────────────────────

test('the turn setup registers the COMPLETE lore section and no other prompt text', async () => {
  // Read the plugin source and pin the persona contract: every prompt
  // section the plugin registers is the complete:true `lore-turn` section —
  // the turn setup carries Lore's per-turn system_prompt (with the skills
  // index concatenated INTO it by assembleTurnPrompt — one source, not a
  // second section), and the fork-seed setup carries the empty placeholder
  // for a seeded session. A second prompt SOURCE (partial section,
  // plugin-authored prose, persona row, identity opener, runtime context) is
  // the two-prompts-drift failure the driver-contract plan fenced against —
  // this fails on the source shape.
  const src = (await import('node:fs')).readFileSync(
    new URL('../src/index.ts', import.meta.url), 'utf8')
  const setups = src.match(/systemPrompt\.section\(\{[\s\S]*?\}\)/g) ?? []
  assert.ok(setups.length >= 1, 'the turn setup registers a prompt section')
  for (const s of setups) {
    assert.match(s, /name: 'lore-turn'/, 'the only section name is lore-turn')
    assert.match(s, /complete: true/, 'the section is COMPLETE (no harness additions)')
  }
  assert.match(src, /text: assembleTurnPrompt\(body\.system_prompt\)/,
    'the turn section text is the handed-over prompt verbatim (dsh\'s tool-skill publishes the catalog), never plugin-authored prose')
  // No persona/identity hooks anywhere in the plugin.
  for (const banned of ['persona:', 'includeHarnessIdentity', 'includeRuntimeContext']) {
    assert.ok(!src.includes(banned), `plugin must not configure ${banned} (preset territory)`)
  }
})

// ─── composition: line B rides the llm-pi-ai `lore` route ────────────────────

test('cordis.patch.yml routes line B through the llm-pi-ai lore route', () => {
  // The model picked in Lore's UI must be the model that runs, and every
  // effort level the gateway advertises must be accepted on the wire:
  // llm-pi-ai's per-model `reasoningEfforts` dict is the only dsh mechanism
  // where the offered levels are configuration (the plugin upserts them from
  // /capabilities), while the old llm-deepseek route hard-coded
  // off/low/high/max and died with UNSUPPORTED_REASONING_EFFORT on anything
  // else. A hand-declared route refuses to resolve with NO models, so the
  // static layer carries ONE bootstrap id (the gate-checked harness model) —
  // the plugin's boot upsert replaces the list wholesale with the
  // accumulated per-model entries.
  const yml = readFileSync(
    join(process.env.DSH_HOME || '/app/home', 'cordis.patch.yml'), 'utf8')
  assert.ok(!yml.includes('lore-gateway'),
    'the lore-gateway route is gone (the pi row falls back to its dormant bare mount)')
  assert.match(yml,
    /- id: llm-pi-ai\n  config:\n    providers:\n      lore:\n        apiKeyEnv: AI_API_KEY\n        api: openai-completions\n        baseURL: !!js process\.env\.LORE_AI_API_URL\n        compat:\n          supportsDeveloperRole: false\n          maxTokensField: max_tokens\n          thinkingFormat: openai\n        defaultContextWindow: !!js Number\(process\.env\.LORE_HARNESS_CONTEXT_WINDOW\)\n        models:\n          - id: !!js process\.env\.LORE_HARNESS_MODEL/,
    'the lore route carries the gateway facts, the deepseek-mirroring compat, and ONE bootstrap model id')
  // compat mirrors what the old route sent and the gateway accepts: role
  // system, max_tokens, bare reasoning_effort (openai thinking format).
  assert.match(yml, /- id: llm-deepseek\n  disabled: true/,
    'the old native adapter is disabled loudly (its default endpoint is the PUBLIC DeepSeek API, and DEEPSEEK_API_KEY is in the env — a soft miss would bill another tenant)')
  // No profile-wide `maxTokens`: the output cap is the router's per-model
  // `max_completion_tokens`, upserted into the catalog entry by the plugin
  // (ensureModelEntry) — a number here re-hardcodes the 8192 cap that cut
  // every model below its real ceiling (plan output-token-cap-from-the-router).
  // The route BLOCK only (comments above it name the word; compat's
  // maxTokensField is the wire spelling, not a cap).
  const routeBlock = yml.split('- id: llm-pi-ai')[1]!.split('- id: llm-deepseek')[0]!
  assert.ok(!/\n\s+maxTokens:/.test(routeBlock),
    'the lore route declares NO maxTokens (per-model catalog upsert owns the cap)')
  assert.ok(!yml.includes('process.env.LORE_AI_API_URL ||'),
    'no dead baseURL fallback: the backend serves no /v1/chat/completions')
  assert.ok(!yml.includes('|| 128000'),
    'no dead context-window fallback: compose already defaults the env')
  assert.match(yml, /- id: agent-default-model\n  config:\n    provider: lore\n    model: !!js process\.env\.LORE_HARNESS_MODEL/,
    'the boot-gated default model points at the lore route')
  assert.match(yml, /- id: session-title-llm\n  config:\n    targetWords: 5\n    targetCjkCharacters: 10\n    maxInputBytes: 4096\n    maxOutputTokens: 64\n    timeoutMs: 60000\n    provider: lore\n    model: !!js process\.env\.CHAT_TITLE_MODEL \|\| process\.env\.CHAT_MODEL/,
    'the titler points at the lore route too')
})

// ─── the provider route string: declared once, shared with the composition ──

test('the provider route key is declared ONCE and matches the composition', () => {
  // index.ts owns the constant. Pinned by source shape, like the persona test
  // above: no harness boot needed to catch a rename that misses the route the
  // composition configures (llm-pi-ai's `lore` provider route).
  const index = readFileSync(new URL('../src/index.ts', import.meta.url), 'utf8')
  assert.match(index, /export const PROVIDER = 'lore'/,
    "the route key is the llm-pi-ai provider route ('lore')")
  assert.ok(!index.includes("'deepseek-official'"), 'index.ts carries no old-route literal')
  assert.ok(!index.includes("'lore-gateway'"), 'index.ts carries no older-route literal')
})

// ─── config gate ──────────────────────────────────────────────────────────────

test('an unset model CRASHES the driver instead of serving a fallback', () => {
  // The composition declares exactly ONE roster entry and cordis.patch.yml
  // reads the same var with no `||` fallback: an unset var used to serve
  // local/orange/chat — the model the default flip was measured AGAINST —
  // so a deploy that forgot HARNESS_MODEL ran the agent on the wrong brain
  // with no error anywhere. apply() calls this at boot.
  const saved = process.env.LORE_HARNESS_MODEL
  try {
    delete process.env.LORE_HARNESS_MODEL
    assert.throws(() => harnessModel(), /LORE_HARNESS_MODEL is required/)
    process.env.LORE_HARNESS_MODEL = '   '
    assert.throws(() => harnessModel(), /LORE_HARNESS_MODEL is required/)
    process.env.LORE_HARNESS_MODEL = 'deepseek/flash'
    assert.equal(harnessModel(), 'deepseek/flash')
  } finally {
    if (saved === undefined) delete process.env.LORE_HARNESS_MODEL
    else process.env.LORE_HARNESS_MODEL = saved
  }
})

// ─── the timeout-policy split (step 9): reads declare a budget, mutations none ─

test('registerLoreTools declares the hang bound ONLY on read-only proxies', () => {
  // A mutating proxy can ASK for approval (approvals.ts parks up to
  // LORE_APPROVAL_MAX_S) — a declared timeoutMs would kill exactly that ask.
  // A read-only proxy never asks: its budget only bounds a HUNG call.
  const registered: any[] = []
  const ctx: any = {
    tools: {
      register: (def: any) => {
        registered.push(def)
        return () => {}
      },
    },
  }
  const mutating = new Set(['edit_document'])
  registerLoreTools(ctx, [
    { function: { name: 'read_document', description: 'r', parameters: {} } },
    { function: { name: 'edit_document', description: 'e', parameters: {} } },
  ], true, mutating)
  const byName = new Map(registered.map((d) => [d.name, d]))
  assert.equal(byName.get('read_document').timeoutMs, READ_TOOL_TIMEOUT_MS)
  assert.equal(byName.get('edit_document').timeoutMs, undefined)
})

// ─── capability: the driver resolves, the payload carries nothing ────────────
// (plan collapse-the-editor-harness-layer step 4). Pinned by source shape,
// like the persona test above: the turn's numbers come from caps.ts, and the
// deleted payload fields (context_window / max_output_tokens) stay deleted.

test('the turn resolves its caps via caps.ts; the payload numbers are gone', async () => {
  const src = readFileSync(new URL('../src/index.ts', import.meta.url), 'utf8')
  assert.match(src, /const caps = await resolveModelCaps\(model\)/,
    'the turn handler resolves the model capability off the ONE source')
  assert.ok(!src.includes('body.context_window'),
    'the payload no longer threads a context window (the deleted Python resolver)')
  assert.ok(!src.includes('body.max_output_tokens'),
    'the payload no longer threads an output cap (the deleted Python resolver)')
  assert.match(src, /req\.method === 'GET' && url\.startsWith\('\/capability'\)/,
    'the /capability endpoint answers the backend vision gate')
})

// ─── context_usage: the contextPressure projection is the counter ────────────
// (plan context-gauge-from-harness-projection). The wire shape {used, cap} is
// unchanged; what changed is WHERE the number comes from — dsh's own occupancy
// accounting, not a local sum of the raw usage chunk.

/** A fake sessionProjections service exposing one contextPressure wire view. */
function fakeProjections(contextPressure: unknown) {
  return {
    snapshot: (_session: unknown) => ({ values: { contextPressure } }),
  }
}

test('contextUsageFrame: projectedTokens wins over pressureTokens', () => {
  const frame = contextUsageFrame(
    fakeProjections({ pressureTokens: 500, projectedTokens: 700, contextWindow: 64000 }),
    {}, 100000)
  assert.deepEqual(frame, { type: 'context_usage', used: 700, cap: 100000 })
})

test('contextUsageFrame: the threaded cap leads; the projection window is the fallback', () => {
  assert.deepEqual(
    contextUsageFrame(
      fakeProjections({ pressureTokens: 500, contextWindow: 64000 }), {}, Number('x')),
    { type: 'context_usage', used: 500, cap: 64000 },
    'an unusable threaded cap falls back to the projection contextWindow')
  assert.equal(
    contextUsageFrame(fakeProjections({ pressureTokens: 500 }), {}, 0), null,
    'no window anywhere — nothing honest to emit')
})

test('contextUsageFrame: absent pressure (or a missing service) emits nothing', () => {
  assert.equal(contextUsageFrame(fakeProjections({}), {}, 100000), null)
  assert.equal(contextUsageFrame(fakeProjections(undefined), {}, 100000), null)
  assert.equal(contextUsageFrame(undefined, {}, 100000), null,
    'a composition without sessionProjections cannot throw mid-turn')
})


// ─── /session-leaf takes the dsh seq (plan collapse-the-editor-harness-layer ──
// step 5: one id space — the ordinal the backend used to send is deleted).

test('the leaf endpoint reads body.seq; the turn ordinal is gone', async () => {
  // Pinned by source shape, like the caps test above: the wire rename is the
  // step's whole contract — a re-grown `body.turn` read is the ordinal id
  // space coming back.
  const src = readFileSync(new URL('../src/index.ts', import.meta.url), 'utf8')
  assert.match(src, /body\.seq === undefined/,
    'the leaf handler requires the seq field (null = root fork)')
  assert.ok(!src.includes('body.turn'),
    'the ordinal turn field is deleted from the leaf wire')
})

// ─── the leaf handler repoints the live subscription on a fork ────────────────
// (plan fork-repoints-live-subscription: a fork's turn must stream to its
// subscriber — the plugin owns the repoint because it owns both the identity
// map and the channel's table). Driven against fakes: no harness boot, the
// channel records repoint calls.

/** A fake standing channel: records repoint calls, nothing else. */
function fakeChannel() {
  const repoints: Array<[string, string, string, number | null]> = []
  return {
    repoints,
    channel: {
      repoint: (loreId: string, from: string, to: string, tail: number | null) => {
        repoints.push([loreId, from, to, tail])
      },
    } as unknown as EventsChannel,
  }
}

/** A fake SessionMap: lore-1 → dsh-a, sets recorded. */
function fakeMap() {
  const sets: Array<[string, string]> = []
  return {
    sets,
    map: {
      get: (loreId: string) => (loreId === 'lore-1' ? 'dsh-a' : loreId),
      set: (loreId: string, dshId: string) => { sets.push([loreId, dshId]) },
    } as any,
  }
}

/** A POST /session-leaf request over an EventEmitter standing in for the
 * IncomingMessage (readBody listens for data/end). */
function leafReq(body: unknown): any {
  const req = new EventEmitter() as any
  req.headers = { 'x-driver-secret': process.env.LORE_DRIVER_SECRET }
  req.method = 'POST'
  req.url = '/session-leaf'
  req.setEncoding = () => {}
  queueMicrotask(() => {
    req.emit('data', JSON.stringify(body))
    req.emit('end')
  })
  return req
}

function leafRes(): any {
  const res: any = {
    status: 0, body: '',
    writeHead(code: number) { res.status = code; return res },
    end(chunk?: string) { res.body = chunk ?? ''; return res },
  }
  return res
}

/** The fake driver ctx: agents.create records its options and resolves,
 * sessions.flush resolves, sessionPersistence serves the parent log. */
function leafCtx(events: any[]) {
  const created: any[] = []
  const ctx: any = {
    agents: {
      create: async (opts: any) => {
        created.push(opts)
        return { agent: { session: opts.sessionId }, dispose: async () => {} }
      },
    },
    sessions: { flush: async () => {} },
    get: () => ({
      open: async () => ({
        read: async () => ({ events }),
        close: async () => {},
      }),
    }),
  }
  return { ctx, created }
}

test('the leaf handler repoints on a REAL fork (tail = the boundary seq) and NOT on the live-tail no-op', async () => {
  const { ctx, created } = leafCtx(log()) // live tail: turn/end seq 9
  const { sets, map } = fakeMap()
  const { repoints, channel } = fakeChannel()

  // Real fork: branch after turn 1 (seq 5 < the live tail 9).
  const forkRes = leafRes()
  await sessionLeaf(ctx, map, channel, leafReq({ session_id: 'lore-1', seq: 5 }), forkRes)
  assert.equal(forkRes.status, 200)
  assert.equal(created.length, 1, 'a fresh session was seeded')
  assert.equal(sets.length, 1, 'the map repointed')
  const [loreId, from, to, tail] = repoints[0]
  assert.equal(loreId, 'lore-1')
  assert.equal(from, 'dsh-a', 'repointed FROM the pre-fork dsh id')
  assert.equal(to, sets[0][1], 'repointed TO the id the map took')
  assert.match(to, /^lore-1~f/, 'the fresh id is the fork spelling')
  assert.equal(tail, 5, "the tail is the seed's last seq — the boundary turn/end")

  // Live-tail no-op (seq = the live tail 9): no seed, no map.set, no repoint.
  const noopRes = leafRes()
  await sessionLeaf(ctx, map, channel, leafReq({ session_id: 'lore-1', seq: 9 }), noopRes)
  assert.equal(noopRes.status, 200)
  assert.equal(created.length, 1, 'no second seed on the live-tail no-op')
  assert.equal(sets.length, 1)
  assert.equal(repoints.length, 1, 'the live-tail no-op does NOT repoint')
})

test('the root fork repoints with a null tail', async () => {
  const { ctx } = leafCtx(log())
  const { sets, map } = fakeMap()
  const { repoints, channel } = fakeChannel()

  const res = leafRes()
  await sessionLeaf(ctx, map, channel, leafReq({ session_id: 'lore-1', seq: null }), res)
  assert.equal(res.status, 200)
  assert.equal(sets.length, 1, 'the map repointed')
  assert.deepEqual(repoints[0], ['lore-1', 'dsh-a', sets[0][1], null],
    'root fork: repoint with tail null (the fresh log is empty)')
})

// ─── /session-leaf `source`: the branch point is the pair (dsh session, seq) ──
// A root fork restarts seqs at 0, so a seq alone is ambiguous across the
// chat's sessions; the log it was stamped from is named beside it.

/** A driver ctx serving one log per dsh session id; records every opened id.
 * An unknown id rejects the way dsh persistence does (`not found`). */
function leafCtxBy(logs: Record<string, any[]>) {
  const created: any[] = []
  const opened: string[] = []
  const ctx: any = {
    agents: {
      create: async (opts: any) => {
        created.push(opts)
        return { agent: { session: opts.sessionId }, dispose: async () => {} }
      },
    },
    sessions: { flush: async () => {} },
    get: () => ({
      open: async (id: string) => {
        opened.push(String(id))
        if (!(String(id) in logs)) throw new Error(`session "${String(id)}" not found`)
        return { read: async () => ({ events: logs[String(id)] }), close: async () => {} }
      },
    }),
  }
  return { ctx, created, opened }
}

/** The current session after a root fork: a fresh log whose seqs restart at 0. */
function rootForkedLog(): any[] {
  return [
    ev(0, 'turn/start', { turn: 1 }),
    userMsg(1, 'Q1-edited'),
    assistantMsg(2, 'B1', 'm1'),
    ev(3, 'turn/end', { reason: { kind: 'completed' } }),
    ev(4, 'turn/start', { turn: 2 }),
    userMsg(5, 'Q2-b'),
    ev(6, 'turn/end', { reason: { kind: 'completed' } }),
  ]
}

test('a seq plus its source seeds from THAT log, not the current one', async () => {
  // dsh-a is current (the root-forked branch); lore-1 is the abandoned one.
  const { ctx, created, opened } = leafCtxBy({ 'dsh-a': rootForkedLog(), 'lore-1': log() })
  const { sets, map } = fakeMap()
  const { repoints, channel } = fakeChannel()

  const res = leafRes()
  await sessionLeaf(ctx, map, channel,
    leafReq({ session_id: 'lore-1', seq: 5, source: 'lore-1' }), res)
  assert.equal(res.status, 200)
  assert.deepEqual(opened, ['lore-1'], 'only the source log is read')
  assert.equal(created.length, 1)
  assert.deepEqual(created[0].seed, log().slice(0, 6),
    "the seed is the SOURCE's prefix through its seq-5 turn/end")
  assert.equal(String(created[0].meta.parentSession), 'lore-1', 'the fork descends from the source')
  assert.deepEqual(repoints[0], ['lore-1', 'dsh-a', sets[0][1], 5],
    'the live subscription still moves FROM the current session')
})

test('a seq without source resolves against current and 422s when absent there', async () => {
  const { ctx, created, opened } = leafCtxBy({ 'dsh-a': rootForkedLog(), 'lore-1': log() })
  const { sets, map } = fakeMap()
  const { repoints, channel } = fakeChannel()

  const res = leafRes()
  await sessionLeaf(ctx, map, channel, leafReq({ session_id: 'lore-1', seq: 9 }), res)
  assert.equal(res.status, 422, 'seq 9 is a turn/end only in the abandoned log')
  assert.deepEqual(opened, ['dsh-a'], 'legacy: the current log is the one read')
  assert.equal(created.length, 0)
  assert.equal(sets.length, 0)
  assert.equal(repoints.length, 0)
})

test("a seq equal to current's live tail but from another source forks, never no-ops", async () => {
  // Seq 6 is current's live tail AND a mid-log turn/end of the abandoned branch.
  const abandoned = [
    ev(0, 'turn/start', { turn: 1 }),
    userMsg(1, 'Q1'),
    assistantMsg(2, 'A1', 'm1'),
    ev(3, 'turn/end', { reason: { kind: 'completed' } }),
    ev(4, 'turn/start', { turn: 2 }),
    userMsg(5, 'Q2'),
    ev(6, 'turn/end', { reason: { kind: 'completed' } }),
    ev(7, 'turn/start', { turn: 3 }),
    userMsg(8, 'Q3'),
    ev(9, 'turn/end', { reason: { kind: 'completed' } }),
  ]
  const { ctx, created } = leafCtxBy({ 'dsh-a': rootForkedLog(), 'lore-1~fold0001': abandoned })
  const { sets, map } = fakeMap()
  const { repoints, channel } = fakeChannel()

  const res = leafRes()
  await sessionLeaf(ctx, map, channel,
    leafReq({ session_id: 'lore-1', seq: 6, source: 'lore-1~fold0001' }), res)
  assert.equal(res.status, 200)
  assert.equal(created.length, 1, 'a real fork, not the live-tail no-op')
  assert.deepEqual(created[0].seed, abandoned.slice(0, 7))
  assert.equal(sets.length, 1)
  assert.equal(repoints.length, 1)
})

test('source equal to the current session keeps the live-tail no-op', async () => {
  const { ctx, created } = leafCtxBy({ 'lore-1~fcur0001': rootForkedLog() })
  const { sets } = fakeMap()
  const map: any = { get: () => 'lore-1~fcur0001', set: (l: string, d: string) => { sets.push([l, d]) } }
  const { repoints, channel } = fakeChannel()

  const res = leafRes()
  await sessionLeaf(ctx, map, channel,
    leafReq({ session_id: 'lore-1', seq: 6, source: 'lore-1~fcur0001' }), res)
  assert.equal(res.status, 200)
  assert.equal(created.length, 0, 'no seed at the live tail')
  assert.equal(sets.length, 0)
  assert.equal(repoints.length, 0)
})

test("a source outside this chat's sessions is refused, and so is a missing one", async () => {
  const { ctx, created, opened } = leafCtxBy({ 'dsh-a': rootForkedLog(), 'lore-2': log() })
  const { sets, map } = fakeMap()
  const { repoints, channel } = fakeChannel()

  for (const source of ['lore-2', 'lore-10', 'lore-2~fabc', 7]) {
    const res = leafRes()
    await sessionLeaf(ctx, map, channel,
      leafReq({ session_id: 'lore-1', seq: 5, source }), res)
    assert.equal(res.status, 422, `source ${JSON.stringify(source)} must be refused`)
  }
  assert.deepEqual(opened, [], "a foreign source's log is never opened")

  const missing = leafRes()
  await sessionLeaf(ctx, map, channel,
    leafReq({ session_id: 'lore-1', seq: 5, source: 'lore-1~fgone000' }), missing)
  assert.equal(missing.status, 422, 'a missing source log is unresolvable, not a 500')
  assert.equal(created.length, 0)
  assert.equal(sets.length, 0)
  assert.equal(repoints.length, 0)
})

test('the driver never deletes a dsh session log (abandoned branches stay continuable)', () => {
  // A `source` names an abandoned session; the pair contract holds only while
  // that log is on disk. Pinned by source shape across the plugin.
  const dir = new URL('../src/', import.meta.url)
  const files = readdirSync(dir).filter((f) => f.endsWith('.ts'))
  assert.ok(files.includes('index.ts'), 'the walk reads the real plugin sources')
  for (const f of files) {
    const src = readFileSync(new URL(f, dir), 'utf8')
    assert.doesNotMatch(src, /\bunlink(Sync)?\(|\brmSync\(|\brm\(|sessions\.delete\(|persistence\.delete\(/,
      `${f} must not delete session logs`)
  }
})

test('a compacted source seeds its POSITIONAL prefix through the boundary row', async () => {
  // In-place compaction: a checkpoint replaced turn 1's rows, and seq-less
  // companion rows sit between seq'd events. The seed keeps both.
  const checkpoint = ev(10, 'user/message',
    { role: 'user', content: [{ type: 'text', text: 'checkpoint' }] },
    { op: 'replace', start: 1, end: 5 })
  const companion = { type: 'tool-call-chunks', data: { batch: [] } }
  const compacted = [
    ev(0, 'session/title', { title: 't' }),
    checkpoint,
    ev(11, 'compaction/end', { compactionId: 'cpt', turn: 2 }),
    ev(6, 'turn/start', { turn: 2 }),
    userMsg(7, 'Q2'),
    companion,
    assistantMsg(8, 'A2', 'm2'),
    ev(9, 'turn/end', { reason: { kind: 'completed' } }),
    ev(12, 'turn/start', { turn: 3 }),
    userMsg(13, 'Q3'),
    ev(14, 'turn/end', { reason: { kind: 'completed' } }),
  ]
  const { ctx, created } = leafCtxBy({ 'dsh-a': rootForkedLog(), 'lore-1~fcmp0001': compacted })
  const { map } = fakeMap()
  const { channel } = fakeChannel()

  const res = leafRes()
  await sessionLeaf(ctx, map, channel,
    leafReq({ session_id: 'lore-1', seq: 9, source: 'lore-1~fcmp0001' }), res)
  assert.equal(res.status, 200)
  assert.deepEqual(created[0].seed, compacted.slice(0, 8),
    'rows in log order through the seq-9 turn/end, companion row included')
})

// ─── reasoning effort: dsh's own model-selection seam (plan ───────────────────
// reasoning-effort-selector step 3). The turn builds a per-HTTP-turn selection
// from the CURRENT body and installs it in the agent setup; the first three
// tests drive the REAL installModelSelection through a minimal waterfall
// context, the last pins the plugin wiring by source shape.

/** A minimal agent-scoped context capturing the two waterfalls
 * installModelSelection registers, so a test can drive them in the order
 * the live loop does (assembly snapshots the selection, then request
 * routing applies it). */
function selectionAgentCtx() {
  const listeners = new Map<string, (...args: any[]) => any>()
  return {
    ctx: {
      on: (kind: string, cb: (...args: any[]) => any) => {
        listeners.set(kind, cb)
        return () => listeners.delete(kind)
      },
    } as any,
    /** Prompt assembly (3-arg waterfall): snapshots selection.current. */
    fireAssemble: () => listeners.get('system-prompt/assemble')!(
      {}, {}, async () => ({ variables: {} })),
    /** Request routing (2-arg waterfall): applies the snapshot. */
    fireRequest: (resolved: Record<string, unknown>) =>
      listeners.get('agent/request')!({}, async () => resolved),
  }
}

test('reasoning effort: an explicit body effort reaches the request config (set)', async () => {
  const h = selectionAgentCtx()
  const selection: ModelSelectionRef = {
    current: {
      provider: PROVIDER,
      model: 'deepseek/pro',
      reasoningEffort: ReasoningEffortId('low'),
    },
    assembled: undefined,
  }
  installModelSelection(h.ctx, selection)
  await h.fireAssemble()
  const out = await h.fireRequest({
    provider: 'other', model: 'other', maxTokens: 4096,
    reasoningEffort: ReasoningEffortId('max'),
  })
  assert.equal(out.provider, PROVIDER)
  assert.equal(out.model, 'deepseek/pro')
  assert.equal(out.reasoningEffort, 'low',
    'the selected effort replaces the resolved one on the wire')
  assert.equal(out.maxTokens, 4096, 'unrelated request config survives')
})

test('reasoning effort: no body effort keeps the request free of one (omitted)', async () => {
  const h = selectionAgentCtx()
  const selection: ModelSelectionRef = {
    current: { provider: PROVIDER, model: 'deepseek/flash' },
    assembled: undefined,
  }
  installModelSelection(h.ctx, selection)
  await h.fireAssemble()
  const out = await h.fireRequest({ provider: 'other', model: 'other', maxTokens: 4096 })
  assert.ok(!('reasoningEffort' in out),
    'Default sends no explicit effort — no field on the wire, the provider default applies')
  assert.equal(out.model, 'deepseek/flash')
})

test('reasoning effort: a persisted explicit effort is cleared when the turn selects none', async () => {
  const h = selectionAgentCtx()
  const selection: ModelSelectionRef = {
    current: { provider: PROVIDER, model: 'deepseek/flash' },
    assembled: undefined,
  }
  installModelSelection(h.ctx, selection)
  await h.fireAssemble()
  // The resolved config carries an explicit effort — inherited from the
  // composition or a stale source on a RESUMED session. A turn whose body
  // selects none must strip it: switching back to Default leaks nothing.
  const out = await h.fireRequest({
    provider: 'other', model: 'other',
    reasoningEffort: ReasoningEffortId('low'),
  })
  assert.ok(!('reasoningEffort' in out),
    'a previously explicit effort never survives into a Default turn')
})

test('reasoning effort: turn() wires the body effort through the selection, nothing else', async () => {
  // Pinned by source shape, like the caps/leaf tests above: the turn handler
  // reads body.reasoning_effort beside the model with the same string guard,
  // TRANSLATES it to the pi-ai level key (identity for pi-ai's own names,
  // `none` → `off`; an untranslatable spelling goes out verbatim for the
  // adapter to refuse loudly — never a silent Default), brands it into the
  // selection, installs installModelSelection in the agent setup (setup
  // completes before the first followup, so the value reaches the FIRST step
  // of the turn), and agentOptions keeps its {provider, model} shape. The
  // fork path carries no selection — a fork's next turn re-reads the effort
  // off its own body.
  const src = readFileSync(new URL('../src/index.ts', import.meta.url), 'utf8')
  assert.match(src,
    /const effort = typeof body\.reasoning_effort === 'string' && body\.reasoning_effort\s+\? body\.reasoning_effort\s+: null/,
    'the effort is read beside the model (null = Default)')
  assert.match(src, /piEffort = piAiEffortKey\(effort\)/,
    'the gateway spelling is translated to the pi-ai level key')
  assert.match(src, /\.\.\.\(piEffort \? \{ reasoningEffort: ReasoningEffortId\(piEffort\) \} : \{\}\)/,
    'the translated effort rides the selection ONLY when present (absent key = Default)')
  assert.match(src, /installModelSelection\(agentCtx, selection\)/,
    'the turn setup installs the selection')
  assert.match(src, /const agentOptions = \{ provider: PROVIDER, model \}/,
    'agentOptions keeps its {provider, model} runtime shape')
  assert.equal(src.match(/installModelSelection\(/g)?.length, 1,
    'exactly one install site — seedForkSession stays selection-free')
})

// ─── the tool-ctx split + the followup/stop routes: standing identity per
// session, per-request facts per turn; /stop routes into the ONE cancel arm;
// /followup is the driver-owned turn.

test('the tool ctx splits: standing survives between turns, the request half does not', () => {
  setSessionToolCtx('dsh-ctx', {
    loreSessionId: 'lore-1', agentKey: 'k',
    mutating: new Set(['edit_document']), holdable: new Set(), regionTools: new Set(),
  })
  assert.equal(loreTurnCtxFor('dsh-ctx'), undefined,
    'standing alone answers nothing — a tool call outside a turn still errors (plan Decision)')
  stampTurnRequestCtx('dsh-ctx', {
    messageId: 'm1', applyMode: 'confirm', document_id: null, region: null,
  })
  const merged = loreTurnCtxFor('dsh-ctx')
  assert.ok(merged, 'both halves present → the merged turn ctx')
  assert.equal(merged!.loreSessionId, 'lore-1')
  assert.equal(merged!.messageId, 'm1')
  assert.equal(merged!.applyMode, 'confirm')
  clearTurnRequestCtx('dsh-ctx')
  assert.equal(loreTurnCtxFor('dsh-ctx'), undefined,
    'the request half is cleared at the turn end')
  // The next turn stamps its own request facts; the standing half was never
  // re-registered (it is session-scoped — one registration, many turns).
  stampTurnRequestCtx('dsh-ctx', {
    messageId: 'm2', applyMode: 'auto', document_id: 'doc-1', region: { start: 0 },
  })
  const next = loreTurnCtxFor('dsh-ctx')!
  assert.equal(next.messageId, 'm2')
  assert.equal(next.document_id, 'doc-1')
  clearSessionToolCtx('dsh-ctx')
  clearTurnRequestCtx('dsh-ctx')
  assert.equal(loreTurnCtxFor('dsh-ctx'), undefined)
})

test('source shape: /stop routes into the ONE cancel arm; /followup is the driver-owned turn', () => {
  // Pinned by source shape, like the leaf/effort tests above.
  const src = readFileSync(new URL('../src/index.ts', import.meta.url), 'utf8')
  assert.equal(src.match(/agent\.cancel\(/g)?.length, 1,
    'exactly one cancel call site — cancelAgentTurn; POST /stop routes through it')
  assert.match(src, /entry\.cancel\(\)/, 'the /stop handler routes through the shared arm')
  assert.match(src, /turn_in_progress/,
    'a second followup on one session is refused, never queued')
  assert.match(src, /event channel unavailable/,
    'no standing channel → explicit 503, never silent frame loss')
  assert.match(src, /buildUserContent/, 'both turn drivers share ONE user-content prep')
})

test("source shape: the followup task pushes turn_closed in its finally (plan step 7)", () => {
  // The WS transport's terminal signal: a driver-owned turn has no stream whose
  // end signals its close, so the plugin announces the turn's full end AFTER
  // every mapped frame (the halt mint included) — and the backend re-mints it
  // on resync when this push died with the socket.
  const src = readFileSync(new URL('../src/index.ts', import.meta.url), 'utf8')
  const finallyIdx = src.indexOf('} finally {\n      await finish()')
  const push = src.match(/try \{ channel\.push\(dshId, \{ type: 'turn_closed' \}\) \} catch/)
  assert.ok(finallyIdx !== -1, 'the followup task has a finally that finishes the turn')
  assert.ok(push, 'the turn_closed push exists, best-effort (a closed channel is the resync\'s to cover)')
  assert.ok(push!.index !== undefined && push!.index > finallyIdx,
    'turn_closed is pushed in the followup task\'s finally, after finish()')
})

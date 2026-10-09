/**
 * Tests for the Lore driver plugin (run at image build:
 * `node --import tsx/esm --test`, mirroring the retired line-A driver's build-time tests).
 * No harness boot — the fork machinery and the split plugin sources are the
 * units under test here; the dsh→frame relay contract lives in map.test.ts.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

import { createSessionEventTap, type EventsChannel } from '../src/ws-events.ts'
import { createSessionStreamBaselines } from '../src/stream-baselines.ts'
import { newTurnMapState } from '../src/map.ts'
import { READ_TOOL_TIMEOUT_MS, registerLoreTools } from '../src/tools.ts'
import {
  clearSessionToolCtx, clearTurnRequestCtx, loreTurnCtxFor,
  setSessionToolCtx, stampTurnRequestCtx,
} from '../src/tools.ts'
import { installModelSelection } from '@deepseek-ai/dsh-agent'
import type { ModelSelectionRef } from '@deepseek-ai/dsh-agent'
import { ReasoningEffortId } from '@deepseek-ai/dsh-llm'

// http.ts (in the entry graph index.ts pulls in) captures the driver secret
// at MODULE LOAD (const SECRET), so the leaf-handler drive below needs it set
// BEFORE that module evaluates — which a static import cannot do (imports
// hoist above any env assignment). The dynamic import runs after both env
// guards; every other test file keeps plain imports because only this graph
// reads this secret.
process.env.LORE_DRIVER_SECRET ||= 't3st-s3cr3t'
const {
  AGENT_KEY_REF, anthropicRoute, contextUsageFrame, ensureAgentKey,
  ensureModelEntry, ensureTitleConfig, ensureTitleEntry, ensureWebSearch,
  followupTurns, forkModel, loreRoute, PROVIDER, ROUTE_BY_API, sessionEntries,
  sessionFork, turnConfigProblem,
} = await import('../src/index.ts')
import { reasoningEffortsDeclaration } from '../src/caps.ts'
import { WEB_SEARCH_KEY_REF } from '../src/web-search/key.ts'

function ev(seq: number, type: string, data?: any, surfaceOp?: any): any {
  return { seq, type, data, surfaceOp }
}

// Real 0.2.0-rc.2 shapes: surfaceOp is the STRING 'append' or the object
// {op:'replace',startSeq,endSeq}; user/message carries the payload as `data` itself.
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

/** The expected dsh fork seed for a boundary `seq`: the source rows [0..seq]
 * plus dsh's inherited-cut marker at seq + 1 (a `turn/end` boundary is
 * balanced, so buildForkSeed's forked closers add nothing). */
function forkSeed(events: any[], seq: number): any[] {
  return [...events.slice(0, seq + 1), {
    type: 'session/end-seed', seq: seq + 1, time: events[seq].time,
    data: { inherited: true },
  }]
}

test('a real v4 session log is indexed by seq (the read contract the fork relies on)', () => {
  // The fork's boundary guard addresses rows BY SEQ (`events[seq].seq ===
  // seq`), which holds only if a read log is contiguous from 0 — the v4
  // format's own contract ("sequence numbers stay contiguous", dsh session
  // types.ts). Pinned on the REAL recorded fixture so a format change that
  // breaks seq-addressing fails HERE, not as a silently wrong fork.
  const rows = readFileSync(
    join(import.meta.dirname, '../../lore-conversation/test/fixtures/session-turn.jsonl'),
    'utf8').trim().split('\n').map((line) => JSON.parse(line) as any)
  // Row 0 is the header (no seq); persistence's read() returns the events.
  const events = rows.filter((r) => typeof r.seq === 'number')
  assert.ok(events.length > 1, 'the fixture carries real events')
  for (let i = 0; i < events.length; i++) {
    assert.equal(events[i].seq, i, `the event at index ${i} must carry seq ${i}`)
  }
})

test('the turn setup registers the COMPLETE lore section and no other prompt text', async () => {
  // Read the plugin source and pin the persona contract: every prompt
  // section the plugin registers is the complete:true `lore-turn` section —
  // the turn setup carries Lore's per-turn system_prompt (with the skills
  // index concatenated INTO it by assembleTurnPrompt — one source, not a
  // second section), and the fork-seed setup carries the empty placeholder
  // for a seeded session. A second prompt SOURCE (partial section,
  // plugin-authored prose, persona row, identity opener, runtime context) is
  // the two-prompts-drift failure the driver-contract plan fenced against —
  // this fails on the source shape. The sections live in turn.ts (the turn
  // setup) and sessions.ts (the fork seed's setup) since the step-4 split;
  // both are read.
  const src = readFileSync(new URL('../src/turn.ts', import.meta.url), 'utf8')
    + readFileSync(new URL('../src/sessions.ts', import.meta.url), 'utf8')
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
  // else. 0.2.0 shape: llm-pi-ai mounts DORMANT in the composition (its
  // `providers` field is volatile, and a `!!js` expression inside the
  // volatile subtree breaks every settings write — the route cannot live in
  // the patch layer), and the plugin's boot/turn upsert declares the `lore`
  // route beside the model entries (loreRoute in turn.ts + ensureModelEntry
  // in ensure.ts).
  const yml = readFileSync(
    join(process.env.DSH_HOME || '/app/home', 'cordis.patch.yml'), 'utf8')
  assert.ok(!yml.includes('lore-gateway'),
    'the lore-gateway route is gone (the pi row falls back to its dormant bare mount)')
  assert.match(yml, /- id: llm-pi-ai\n  disabled: false/,
    'the pi row stays mounted (dormant — the plugin declares the route through settings)')
  const routeBlock = yml.split('- id: llm-pi-ai')[1]!.split('- id: llm-deepseek')[0]!
  assert.ok(!/providers:/.test(routeBlock),
    'the composition declares NO provider config — env-bound values ride the settings document, never the volatile patch layer')
  assert.ok(!/\n\s+maxTokens:/.test(routeBlock),
    'the lore route declares NO maxTokens (per-model catalog upsert owns the cap)')
  // The route FACTS live in the plugin source now (turn.ts, since the step-4
  // split) — pinned by source shape, like the provider-route test below:
  // apiKeyEnv (the Lore-internal credentials ref), the openai-completions
  // api, the deepseek-mirroring compat trio (role system, max_tokens, bare
  // reasoning_effort), no fallback (no gateway URL skips the scaffold and
  // the turn is refused).
  const routeSrc = readFileSync(new URL('../src/turn.ts', import.meta.url), 'utf8')
  assert.match(routeSrc, /apiKeyEnv: AGENT_KEY_REF,\n    api: 'openai-completions',\n    baseURL,/,
    'loreRoute carries the route facts the dormant row cannot')
  assert.match(routeSrc, /supportsDeveloperRole: false,\n      maxTokensField: 'max_tokens',\n      thinkingFormat: 'openai',/,
    'compat mirrors what the old route sent and the gateway accepts')
  assert.match(routeSrc, /if \(!baseURL\) return null/,
    'no baseURL fallback: the backend serves no /v1/chat/completions')
  assert.match(yml, /- id: llm-deepseek\n  disabled: true/,
    'the old native adapter is disabled loudly (its default endpoint is the PUBLIC DeepSeek API, and DEEPSEEK_API_KEY is in the env — a soft miss would bill another tenant)')
  assert.match(yml, /- id: agent-default-model\n  config:\n    provider: lore\n    model: ''/,
    'the entry stays (dsh injects agentDefaultModel — deleting it breaks activation) with NO env link: no model rides the composition, every turn names its own. The empty string, not undefined — undefined fails the entry\'s own "$.model missing required value" validation (proven on the gray boot)')
  // The MOVED composition: `web` and `session-title-llm` carry no config in
  // the home patch (the config-editor refuses an entry a home patch
  // overrides); their whole config lives in the profile patch — the editor's
  // write target — as STATIC rows the per-turn apply edits.
  const homeDir = process.env.DSH_HOME || '/app/home'
  const profilePatch = readFileSync(join(homeDir, 'profiles/lore/cordis.patch.yml'), 'utf8')
  const homeTitleBlock = yml.split('- id: session-title-llm')[1]!.split('\n- id:')[0]!
  assert.ok(!/config:/.test(homeTitleBlock),
    'the home row carries no titler config — the editor must reach the entry')
  const profileTitleBlock = profilePatch.split('- id: session-title-llm')[1]!.split('- id:')[0]!
  for (const field of ['targetWords: 5', 'targetCjkCharacters: 10', 'maxInputBytes: 4096', 'maxOutputTokens: 2048', 'timeoutMs: 60000']) {
    assert.ok(profileTitleBlock.includes(field), `the profile row restates ${field}`)
  }
  assert.ok(!/provider:/.test(profileTitleBlock) && !/model:/.test(profileTitleBlock),
    'NO committed provider/model pair — the pair is per-turn state (ensureTitleConfig), dsh refuses a half or empty one at activation')
  const homeWebBlock = yml.split('- id: web')[1]!.split('\n- id:')[0]!
  assert.ok(!/config:/.test(homeWebBlock), 'the home web row carries no config either')
  const profileWebBlock = profilePatch.split('- id: web')[1]!.split('- id:')[0]!
  assert.match(profileWebBlock, /searchProvider: deepseek-official/,
    'the static default pin (the per-turn edit moves it on admin change)')
  assert.match(profileWebBlock, /fetchProvider: http/,
    'every other field survives the per-turn edit (the change keeps them)')
  assert.match(yml, /- id: web-search-deepseek\n  disabled: false\n  config:\n    apiKeyEnv: LORE_WEB_SEARCH_KEY/,
    'dsh DeepSeek resolves its key per request through the Lore-internal ref (the per-turn credential write)')
  assert.match(yml, /- id: tool-web\n  disabled: false\n  config:\n    search: true/,
    'web_search is ALWAYS offered — there is no off state; to stop searching an admin clears the key')
})

// ─── the provider route string: declared once, shared with the composition ──

test('the provider route key is declared ONCE and matches the composition', () => {
  // ensure.ts owns the constant (since the step-4 split). Pinned by source
  // shape, like the persona test above: no harness boot needed to catch a
  // rename that misses the route the composition configures (llm-pi-ai's
  // `lore` provider route).
  const ensureSrc = readFileSync(new URL('../src/ensure.ts', import.meta.url), 'utf8')
  assert.match(ensureSrc, /export const PROVIDER = 'lore'/,
    "the route key is the llm-pi-ai provider route ('lore')")
  assert.ok(!ensureSrc.includes("'deepseek-official'"), 'ensure.ts carries no old-route literal')
  assert.ok(!ensureSrc.includes("'lore-gateway'"), 'ensure.ts carries no older-route literal')
})

// ─── config gate ──────────────────────────────────────────────────────────────

test('a turn with no model is refused by name — there is no env fallback', () => {
  // The model comes only from the turn: the backend resolves body.model →
  // session row → admin CHAT_MODEL and refuses a model-less turn itself
  // (completions.py), so the harness's named refusal is the second gate, and
  // turn.ts must carry no LORE_HARNESS_MODEL read at all — wiring, not
  // configuration (plan component-wiring-not-settings step 3).
  const gateSrc = readFileSync(new URL('../src/turn.ts', import.meta.url), 'utf8')
  assert.ok(!gateSrc.includes('LORE_HARNESS_MODEL'),
    'no env model read remains in the plugin')
  const gw = { base: 'http://gw/v1', key: 'k' }
  assert.equal(turnConfigProblem('m', gw), null)
  assert.match(turnConfigProblem('', gw)!, /chat model.*Admin panel → Models & APIs/)
  assert.match(turnConfigProblem('m', { ...gw, base: '' })!, /AI API URL.*Admin panel → Models & APIs/)
  assert.match(turnConfigProblem('m', { ...gw, key: '' })!, /AI API key.*Admin panel → Models & APIs/)
})

// ─── the route default window: a constant, not configuration ──────────────────

test('the route default window is the constant 128000 — no env read', () => {
  // The real window comes from the gateway caps per model (caps.ts); the
  // scaffold's number is the composition fallback for models the gateway
  // leaves bare — the same 128000 the composes carried as
  // LORE_HARNESS_CONTEXT_WINDOW's default, now a constant (plan
  // component-wiring-not-settings step 3).
  const saved = process.env.LORE_HARNESS_CONTEXT_WINDOW
  try {
    delete process.env.LORE_HARNESS_CONTEXT_WINDOW
    assert.equal(loreRoute('http://gw/v1')!.defaultContextWindow, 128000,
      'no env → the composition default')
    process.env.LORE_HARNESS_CONTEXT_WINDOW = '999999'
    assert.equal(loreRoute('http://gw/v1')!.defaultContextWindow, 128000,
      'a set env must not move the route default')
    assert.equal(loreRoute('') , null, 'no baseURL → no route (the turn is refused)')
  } finally {
    if (saved === undefined) delete process.env.LORE_HARNESS_CONTEXT_WINDOW
    else process.env.LORE_HARNESS_CONTEXT_WINDOW = saved
  }
  const routeSrc = readFileSync(new URL('../src/turn.ts', import.meta.url), 'utf8')
  assert.ok(!routeSrc.includes('LORE_HARNESS_CONTEXT_WINDOW'),
    'no env window read remains in the plugin')
})

// ─── the second route: anthropic-messages through the same router ────────────
// The router's /v1/models `api` field picks
// the route per model; `anthropic` is the pi-ai INSTALLED-CATALOG provider id —
// a configured model spreads the installed entry of the same id under it, which
// is the only way the Claude flags dsh withholds from configuration reach a
// model — so the NAME is load-bearing, never a synonym.

test('anthropicRoute: /v1 stripped (the Anthropic SDK appends its own paths), no api, no compat', () => {
  assert.equal(anthropicRoute('http://x/v1')!.baseURL, 'http://x')
  assert.equal(anthropicRoute('http://x/v1/')!.baseURL, 'http://x')
  assert.equal(anthropicRoute('http://x')!.baseURL, 'http://x', 'a base without /v1 passes unchanged')
  assert.equal(anthropicRoute(''), null, 'no baseURL → no route (the same refusal path as loreRoute)')
  const route = anthropicRoute('http://x/v1')!
  assert.equal(route.apiKeyEnv, AGENT_KEY_REF)
  assert.equal(route.defaultContextWindow, 128000,
    'the router leaves this model bare — without the default, dsh\'s schema default 262144 applies')
  assert.ok(!('api' in route),
    'NO api: the route name IS the catalog provider id — the catalog\'s shared anthropic-messages applies')
  assert.ok(!('compat' in route),
    'NO route compat: compat is per model, the router\'s own (the installed entry supplies the catalog\'s)')
})

test('ROUTE_BY_API: one map — the caps api names the route', () => {
  assert.equal(ROUTE_BY_API['anthropic-messages'], 'anthropic')
  assert.equal(ROUTE_BY_API['openai-completions'], 'lore')
})

/** A duck-typed settings seam holding several provider routes; `update`
 * records the patch verbatim and deep-merges like the real service. */
function fakeMultiRouteSettings() {
  const updates: Array<[unknown, Record<string, unknown>]> = []
  const providers: Record<string, Record<string, unknown>> = {}
  return {
    updates, providers,
    describe: () => [{ ns: 'llm-pi-ai', value: { providers } }],
    update: async (ns: unknown, patch: Record<string, unknown>) => {
      updates.push([ns, structuredClone(patch)])
      for (const [name, section] of Object.entries(patch.providers as Record<string, Record<string, unknown>>)) {
        providers[name] = { ...(providers[name] ?? {}), ...section }
      }
    },
  }
}

test('an anthropic-api model lands under providers.anthropic with the scaffold and its compat', async () => {
  const settings = fakeMultiRouteSettings()
  await ensureModelEntry(settings as never, 'deepseek-a/flash', 524288, 131072, false,
    ['low', 'high', 'max'], false, anthropicRoute('http://x/v1'),
    { forceAdaptiveThinking: true }, 'anthropic')
  assert.deepEqual(settings.updates[0]![1], {
    providers: { anthropic: {
      apiKeyEnv: 'LORE_AGENT_API_KEY',
      baseURL: 'http://x',
      defaultContextWindow: 128000,
      models: [{
        id: 'deepseek-a/flash',
        reasoningEfforts: { low: 'low', high: 'high', max: 'max' },
        compat: { forceAdaptiveThinking: true },
        contextWindow: 524288, maxTokens: 131072,
      }],
    } },
  }, 'no api and no route compat on the section — the catalog name carries the protocol; the compat rides the MODEL entry')
})

test('a compat the router adds later lands: a committed entry whose only difference is a new compat IS rewritten', async () => {
  const settings = fakeMultiRouteSettings()
  await ensureModelEntry(settings as never, 'deepseek-a/flash', 524288, 131072, false,
    null, false, anthropicRoute('http://x/v1'), null, 'anthropic')
  assert.equal(settings.updates.length, 1)
  await ensureModelEntry(settings as never, 'deepseek-a/flash', 524288, 131072, false,
    null, false, anthropicRoute('http://x/v1'), null, 'anthropic')
  assert.equal(settings.updates.length, 1, 'identical values skip the write (today\'s rule, compat included)')
  await ensureModelEntry(settings as never, 'deepseek-a/flash', 524288, 131072, false,
    null, false, anthropicRoute('http://x/v1'), { forceAdaptiveThinking: true }, 'anthropic')
  assert.equal(settings.updates.length, 2, 'the new compat alone is a difference')
  assert.deepEqual(settings.providers.anthropic!.models, [{
    id: 'deepseek-a/flash', contextWindow: 524288, maxTokens: 131072,
    compat: { forceAdaptiveThinking: true },
  }])
  // And back: dropping the compat rewrites too (the router owns the say).
  await ensureModelEntry(settings as never, 'deepseek-a/flash', 524288, 131072, false,
    null, false, anthropicRoute('http://x/v1'), null, 'anthropic')
  assert.deepEqual(settings.providers.anthropic!.models,
    [{ id: 'deepseek-a/flash', contextWindow: 524288, maxTokens: 131072 }])
})

test('no cross-route removal: the same model already under lore stays there untouched', async () => {
  const settings = fakeMultiRouteSettings()
  await ensureModelEntry(settings as never, 'm/x', 128000, 65536, false, null,
    false, loreRoute('http://gw/v1'))
  // The router moves m/x to the anthropic protocol.
  await ensureModelEntry(settings as never, 'm/x', 128000, 65536, false, null,
    false, anthropicRoute('http://x/v1'), null, 'anthropic')
  assert.deepEqual((settings.providers.lore!.models as any[]).map((m) => m.id), ['m/x'],
    'the stale lore entry stays — a Lore selection always names the provider explicitly, and emptying a route dsh refuses (resolves no models) would fail the whole settings write')
  assert.deepEqual((settings.providers.anthropic!.models as any[]).map((m) => m.id), ['m/x'])
})

test('an openai model on lore produces today\'s payload unchanged', async () => {
  const settings = fakeMultiRouteSettings()
  await ensureModelEntry(settings as never, 'm/x', 128000, 65536, true,
    ['low'], false, loreRoute('http://gw/v1'))
  assert.deepEqual(settings.updates[0]![1], {
    providers: { lore: {
      apiKeyEnv: 'LORE_AGENT_API_KEY',
      api: 'openai-completions',
      baseURL: 'http://gw/v1',
      compat: { supportsDeveloperRole: false, maxTokensField: 'max_tokens', thinkingFormat: 'openai' },
      defaultContextWindow: 128000,
      models: [{
        id: 'm/x', input: ['text', 'image'], reasoningEfforts: { low: 'low' },
        contextWindow: 128000, maxTokens: 65536,
      }],
    } },
  })
})

test('catalog binding: the route name anthropic resolves through the installed pi-ai catalog', async () => {
  // Proves the load-bearing name against the REAL catalog resolver: a catalog
  // id inherits the installed entry's compat (the withheld Claude flags), a
  // non-catalog id still resolves via the catalog's shared route api. The
  // scaffold is the production anthropicRoute; the two route defaults the
  // settings schema would fill (resolveRouteModels takes a parsed request)
  // are the only fields added here.
  const { resolveRouteModels } = await import('../../packages/llm/llm-pi-ai/src/catalog.ts')
  const scaffold = {
    defaultMaxTokens: 131072, defaultInput: ['text'],
    ...anthropicRoute('http://x/v1')!,
  }
  const efforts = reasoningEffortsDeclaration(['low', 'medium', 'high', 'xhigh', 'max'], 'claude-opus-5-5')!
  const opus = resolveRouteModels({
    provider: 'anthropic', ...scaffold,
    models: [{ id: 'claude-opus-5-5', reasoningEfforts: efforts }],
  }).models.find((m: any) => m.id === 'claude-opus-5-5')
  assert.equal(opus.api, 'anthropic-messages')
  assert.equal(opus.baseUrl, 'http://x', 'the route\'s baseURL overrides the catalog\'s own')
  assert.equal(opus.compat.supportsMidConvoEffort, true,
    'the withheld flag reaches the model only through the installed entry of the same id')
  assert.equal(opus.compat.forceAdaptiveThinking, true)
  // A withheld key written bare is refused loudly, naming it.
  assert.throws(
    () => resolveRouteModels({
      provider: 'anthropic', ...scaffold,
      models: [{ id: 'claude-opus-5-5', compat: { supportsMidConvoEffort: true } }],
    }),
    /not configurable here/,
    'the router\'s compat allow-list is proven against the real gate')
  const ds = resolveRouteModels({
    provider: 'anthropic', ...scaffold,
    models: [{ id: 'deepseek-a/flash', compat: { forceAdaptiveThinking: true } }],
  }).models.find((m: any) => m.id === 'deepseek-a/flash')
  assert.equal(ds.api, 'anthropic-messages',
    'a non-catalog id takes the catalog\'s shared route api (routeApi)')
  assert.equal(ds.contextWindow, 128000,
    'no window anywhere else — the scaffold default applies')
})

test('a fork seeds agentOptions with the parent header\'s own provider', async () => {
  const events = [
    ev(0, 'turn/start', { turn: 1 }),
    ev(1, 'request/header', { header: { config: { provider: 'anthropic', model: 'deepseek-a/flash' } } }),
    userMsg(2, 'Q1'),
    assistantMsg(3, 'A1', 'deepseek-a/flash'),
    ev(4, 'turn/end', { reason: { kind: 'completed' } }),
    ev(5, 'turn/start', { turn: 2 }),
    userMsg(6, 'Q2'),
    ev(7, 'assistant/chunk', { chunk: { type: 'text-delta', text: 'A2' } }),
    ev(8, 'turn/end', { reason: { kind: 'completed' } }),
  ]
  // leafCtxBy (not leafCtx): the existence probe OPENS new_lore_id — the
  // one-log fake would answer "exists" and never seed.
  const { ctx, created } = leafCtxBy({ 'dsh-a': events })
  const { map } = fakeMap()
  const res = leafRes()
  await sessionFork(ctx, map,
    forkReq({ source_dsh_id: 'dsh-a', seq: 4, new_lore_id: 'lore-branch-2' }), res)
  assert.equal(res.status, 200)
  assert.deepEqual(created[0].agentOptions,
    { provider: 'anthropic', model: 'deepseek-a/flash' },
    'the fork seeds the pair off the parent\'s request/header — the next turn re-selects its own')
})


// ─── the agent key: dsh credentials, written only on difference ──────────────

function fakeCredentials(stored?: string) {
  const values = new Map<string, string>(stored === undefined ? [] : [[AGENT_KEY_REF, stored]])
  const sets: [string, string | undefined][] = []
  return {
    sets,
    resolve: async (ref: string) => (values.has(ref) ? { value: values.get(ref)! } : undefined),
    set: async (ref: string, value: string) => {
      sets.push([ref, value])
      values.set(ref, value)
    },
    unset: async (ref: string) => {
      sets.push([ref, undefined])
      values.delete(ref)
    },
  }
}

test('the turn key is stored under the internal ref only when it differs', async () => {
  const creds = fakeCredentials()
  await ensureAgentKey(creds, 'sk-admin')
  await ensureAgentKey(creds, 'sk-admin')
  assert.deepEqual(creds.sets, [['LORE_AGENT_API_KEY', 'sk-admin']],
    'one write for a new key, none for the same key')
  await ensureAgentKey(creds, 'sk-rotated')
  assert.deepEqual(creds.sets.at(-1), ['LORE_AGENT_API_KEY', 'sk-rotated'])
  await assert.rejects(ensureAgentKey(undefined, 'sk'), /credentials service is not composed/)
})

// ─── the per-turn admin config: search pin + credential, title pair ──────────

/** A duck-typed configEditor: rows by id, `edit` recorded and applied. */
function fakeConfigEditor(rows: Record<string, Record<string, unknown>>) {
  const edits: Array<{ id: string, next: Record<string, unknown> }> = []
  return {
    edits,
    entries: () => Object.entries(rows).map(([id, config]) => ({ options: { id, config } })),
    edit: async (
      entry: { options: { id: string } },
      change: (current: Record<string, unknown>) => Record<string, unknown>,
    ) => {
      const id = entry.options.id
      const next = change(structuredClone(rows[id] ?? {}))
      edits.push({ id, next })
      rows[id] = next
    },
  }
}

const TITLE_FIXED = {
  targetWords: 5, targetCjkCharacters: 10, maxInputBytes: 4096,
  maxOutputTokens: 2048, timeoutMs: 60000,
}

test('a changed provider edits web exactly once, keeping every other field; the same value again edits nothing', async () => {
  const editor = fakeConfigEditor({
    web: { searchProvider: 'deepseek-official', fetchProvider: 'http' },
  })
  const creds = fakeCredentials()
  await ensureWebSearch(editor, creds, 'lore-brave', 'brave-key')
  await ensureWebSearch(editor, creds, 'lore-brave', 'brave-key')
  assert.deepEqual(editor.edits, [
    { id: 'web', next: { searchProvider: 'lore-brave', fetchProvider: 'http' } },
  ])
})

test('the credential lands under the ONE ref only when it differs — empty UNSETS, never stores', async () => {
  const editor = fakeConfigEditor({ web: { searchProvider: 'deepseek-official', fetchProvider: 'http' } })
  const creds = fakeCredentials()
  // First turn on a fresh install: nothing stored, the payload says '' — a
  // missing ref IS the empty credential (the loud no-key failure).
  await ensureWebSearch(editor, creds, 'deepseek-official', '')
  await ensureWebSearch(editor, creds, 'deepseek-official', '')
  assert.deepEqual(creds.sets, [], 'no write and no unset for an already-absent key')
  // An admin sets a key: one write.
  await ensureWebSearch(editor, creds, 'deepseek-official', 'sk-deepseek')
  assert.deepEqual(creds.sets, [[WEB_SEARCH_KEY_REF, 'sk-deepseek']])
  // An admin clears the key: the off switch UNSETS the ref (credentials-local
  // refuses to store '').
  await ensureWebSearch(editor, creds, 'deepseek-official', '')
  assert.deepEqual(creds.sets.at(-1), [WEB_SEARCH_KEY_REF, undefined])
  assert.deepEqual(editor.edits, [], 'an unchanged provider never edits, whatever the key does')
})

test('a missing seam fails loud — the pin and the key are not optional', async () => {
  const editor = fakeConfigEditor({ web: {} })
  await assert.rejects(
    ensureWebSearch(undefined, fakeCredentials(), 'lore-brave', 'k'),
    /config editor is not composed/)
  await assert.rejects(
    ensureWebSearch(editor, undefined, 'lore-brave', 'k'),
    /credentials service is not composed/)
})

test('a title change edits session-title-llm with the pair; an empty title removes it; no change edits nothing', async () => {
  const editor = fakeConfigEditor({ 'session-title-llm': { ...TITLE_FIXED } })
  await ensureTitleConfig(editor, 'local/orange/titler')
  await ensureTitleConfig(editor, 'local/orange/titler')
  assert.deepEqual(editor.edits, [
    { id: 'session-title-llm', next: { ...TITLE_FIXED, provider: 'lore', model: 'local/orange/titler' } },
  ], 'one edit for a change, none for the same value')
  // The provider param names the TITLE model's own route (an anthropic-api
  // title model rides providers.anthropic).
  await ensureTitleConfig(editor, 'deepseek-a/flash', 'anthropic')
  assert.deepEqual(editor.edits.at(-1)!.next,
    { ...TITLE_FIXED, provider: 'anthropic', model: 'deepseek-a/flash' })
  await ensureTitleConfig(editor, 'deepseek-a/flash', 'anthropic')
  assert.equal(editor.edits.length, 2, 'the same pair edits nothing')
  await ensureTitleConfig(editor, '')
  assert.deepEqual(editor.edits.at(-1)!.next, TITLE_FIXED,
    'an empty title_model removes the pair — the titler rides the session\'s own route')
  await ensureTitleConfig(editor, '')
  assert.equal(editor.edits.length, 3, 'removing an absent pair edits nothing')
  await assert.rejects(ensureTitleConfig(undefined, 'm'), /config editor is not composed/)
})

// ─── the title model: a catalog entry, a warning never an exit ──────────────

function fakeCatalogSettings() {
  const updates: [unknown, Record<string, unknown>][] = []
  const lore: Record<string, unknown> = { baseURL: 'http://gw/v1', apiKeyEnv: 'LORE_AGENT_API_KEY', models: [] }
  return {
    updates,
    describe: () => [{ ns: 'llm-pi-ai', value: { providers: { lore } } }],
    update: async (ns: unknown, patch: Record<string, unknown>) => {
      updates.push([ns, patch])
      Object.assign(lore, (patch.providers as { lore: Record<string, unknown> }).lore)
    },
  }
}

test('the title model is catalogued once, a reasoner like any other', async () => {
  // pi-ai refuses an id outside the catalog, and the composition already
  // points the titler at the model — an uncatalogued title model fails titles.
  const route = { apiKeyEnv: 'LORE_AGENT_API_KEY', api: 'openai-completions', baseURL: 'http://gw/v1', compat: {} }
  const settings = fakeCatalogSettings()
  await ensureTitleEntry(settings, 'titler/plain', 'lore', route)
  await ensureTitleEntry(settings, 'titler/plain', 'lore', route)
  assert.equal(settings.updates.length, 1, 'the committed entry writes nothing')
  await ensureTitleEntry(settings, 'titler/thinks', 'lore', route)
  assert.equal(settings.updates.length, 2)
})

// ─── the fork model: the parent's last requested route ────────────────────────

test('a fork seeds with the pair the parent last requested at the branch point', () => {
  const header = (seq: number, model: string, provider = 'lore') =>
    ev(seq, 'request/header', { header: { config: { provider, model } } })
  const events = [header(0, 'm/first'), ev(1, 'turn/end'), header(2, 'm/second', 'anthropic'), ev(3, 'turn/end')]
  assert.deepEqual(forkModel(events, 1), { provider: 'lore', model: 'm/first' },
    'a header past the branch point is not the parent\'s')
  assert.deepEqual(forkModel(events, 3), { provider: 'anthropic', model: 'm/second' },
    'the provider rides the same header (providerForOpenStep\'s fact) — a fork off an anthropic-protocol turn seeds its own route')
  assert.deepEqual(forkModel([ev(0, 'turn/end')], 0), { provider: 'lore', model: '' },
    'no header → today\'s value (the next turn selects its own model either way)')
  assert.deepEqual(
    forkModel([ev(0, 'request/header', { header: { config: { model: 'm/bare' } } })], 0),
    { provider: 'lore', model: 'm/bare' },
    'a header naming a model but no provider keeps the default route')
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
  // The caps read lives in turn.ts (prepareTurnDrive) and the /capability
  // route in http.ts (handleRequest) since the step-4 split — both read.
  const src = readFileSync(new URL('../src/turn.ts', import.meta.url), 'utf8')
    + readFileSync(new URL('../src/http.ts', import.meta.url), 'utf8')
  assert.match(src, /const caps = await resolveModelCaps\(model, \{ gateway \}\)/,
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


/** A fake standing channel: records repoint calls, nothing else. The
 * EventsChannel interface has no repoint anymore (plan chat-branch-sessions
 * step 4) — the recorder stays as a tripwire: nothing may ever re-grow a
 * re-point on the fork paths. */
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

/** A POST driver-endpoint request over an EventEmitter standing in for the
 * IncomingMessage (readBody listens for data/end). */
function leafReq(body: unknown): any {
  const req = new EventEmitter() as any
  req.headers = { 'x-driver-secret': process.env.LORE_DRIVER_SECRET }
  req.method = 'POST'
  req.url = '/session-entries'
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

// ─── /session-entries balances a crashed log with dsh's own closers ──────────
// The regression this section pins: without dsh's interrupted-turn closers a
// driver crash mid-turn replays as a turn open FOREVER — the halt card waits
// for the backend's no-progress deadline. The read appends
// `interruptedTurnClosers` for a NON-live session, in memory only — the same
// deterministic closers dsh's cold read (readColdSessionLog) and its resume
// repair produce. "Live" is the REGISTERED driver-owned turn: the dsh id is a
// key of `followupTurns` — never "an agent might be running". The SAME
// liveness gates the replayed transport terminal: a
// non-live session's every closed turn ends with a seq-anchored turn_closed.

/** log() with turn 2 left open: the last row is seq 8, no turn/end — a
 * SIGKILL mid-turn leaves exactly this shape on disk. */
function crashedLog(): any[] {
  return log().slice(0, 9) // events 0..8: turn 1 closed at 5, turn 2 open
}

/** A POST /session-entries request (same req shape the leaf tests use). */
function entriesReq(body: unknown): any {
  const req = leafReq(body)
  req.url = '/session-entries'
  return req
}

async function readEntries(ctx: any, map: any): Promise<any> {
  const res = leafRes()
  await sessionEntries(ctx, map, createSessionStreamBaselines(),
    entriesReq({ session_id: 'lore-1' }), res)
  assert.equal(res.status, 200)
  return JSON.parse(res.body)
}

test('a crashed log read for a NON-live session yields a CLOSED turn + the halt mint', async () => {
  // No turn is registered for dsh-a in followupTurns — the driver is dead.
  const { ctx } = leafCtx(crashedLog())
  const { map } = fakeMap()
  const reply = await readEntries(ctx, map)

  assert.equal(reply.turns.length, 2)
  assert.equal(reply.turns[0].end_seq, 5, 'the settled turn is untouched')
  const crashed = reply.turns[1]
  // The closer seq is deterministic: it continues the log (last seq 8 → 9),
  // which is what makes the channel's stamp later match dsh's persisted repair.
  assert.equal(crashed.end_seq, 9,
    'the interrupted turn closes at the deterministic closer seq')
  const end = crashed.frames.find(
    (f: any) => f.type === 'dsh_event' && f.kind === 'turn/end')
  assert.equal(end.data.reason.kind, 'interrupted')
  const halt = crashed.frames.at(-2)
  assert.equal(halt.type, 'lore/halt', 'mapEvent mints the halt beside the turn/end')
  assert.equal(halt.seq, 9.7)
  assert.equal(halt.data.reason, 'interrupted')
  const terminal = crashed.frames.at(-1)
  assert.equal(terminal.type, 'turn_closed',
    'the non-live replay carries the transport terminal the dead push cannot')
  assert.equal(terminal.seq, 9.9, 'anchored after the halt mint (9.7) — the terminal orders last')
  assert.equal(reply.tail_seq, 9,
    'the projection folds the closers like any row — the high-water mark is the synthetic turn/end')
})

test('the same log with the id in followupTurns stays OPEN — liveness is the REGISTERED turn', async () => {
  const { ctx } = leafCtx(crashedLog())
  const { map } = fakeMap()
  followupTurns.set('dsh-a', { cancel: () => {} })
  try {
    const reply = await readEntries(ctx, map)
    const live = reply.turns.at(-1)
    assert.equal(live.end_seq, undefined,
      'a registered driver-owned turn replays open — closers on it would close a RUNNING turn in every replay')
    assert.ok(!live.frames.some((f: any) => f.type === 'lore/halt'),
      'no halt card for a turn that is still streaming')
    assert.ok(!reply.turns.some((t: any) =>
      t.frames.some((f: any) => f.type === 'turn_closed')),
      'a live session replays no terminals — the running turn closes live')
  } finally {
    followupTurns.delete('dsh-a')
  }
})

test('a balanced log gains nothing: the closers of a settled log are empty', async () => {
  const { ctx } = leafCtx(log()) // both turns ended
  const { map } = fakeMap()
  const reply = await readEntries(ctx, map)
  assert.equal(reply.turns.length, 2)
  assert.equal(reply.turns[1].end_seq, 9)
  assert.ok(!reply.turns.some((t: any) =>
    t.frames.some((f: any) => f.type === 'lore/halt')),
    'completed turns mint no halt — and the read appended no synthetic rows')
  assert.equal(reply.turns[1].frames.length, 5,
    'turn 2 replays exactly its four mapped rows + the transport terminal')
  assert.deepEqual(reply.turns[1].frames.at(-1), { type: 'turn_closed', seq: 9.9 })
})

test('a NON-live read replays the transport terminal on every closed turn', async () => {
  // The browser's only terminal, carried by the replay for the same session
  // class the closers balance: the plugin's live push is best-effort and a
  // socket gap loses it, so a resync of a non-live session re-delivers each
  // closed turn's terminal (seq-anchored after the turn's last mint).
  const { ctx } = leafCtx(log())
  const { map } = fakeMap()
  const reply = await readEntries(ctx, map)
  assert.deepEqual(reply.turns[0].frames.at(-1), { type: 'turn_closed', seq: 5.9 })
  assert.deepEqual(reply.turns[1].frames.at(-1), { type: 'turn_closed', seq: 9.9 })
})

test('a LIVE session replays no terminals even for CLOSED turns', async () => {
  // Liveness is the REGISTERED turn: while one runs, a previous turn's lost
  // push is the NEWER turn's registration to supersede, never the replay's to
  // deliver (the plugin's own push owns the live path).
  const { ctx } = leafCtx(log())
  const { map } = fakeMap()
  followupTurns.set('dsh-a', { cancel: () => {} })
  try {
    const reply = await readEntries(ctx, map)
    assert.ok(!reply.turns.some((t: any) =>
      t.frames.some((f: any) => f.type === 'turn_closed')))
  } finally {
    followupTurns.delete('dsh-a')
  }
})

test('a compacted source seeds its prefix with the checkpoint row verbatim', async () => {
  // v4 compaction is append-only: the checkpoint row REPLACES rows 1..4 on
  // the message surface (surfaceOp replace) but sits at its OWN seq — the
  // log stays contiguous. The seed through a post-compaction turn/end keeps
  // the checkpoint row and its replace window verbatim inside the prefix.
  const compacted = [
    ev(0, 'session/title', { title: 't' }),
    ev(1, 'turn/start', { turn: 1 }),
    userMsg(2, 'Q1'),
    assistantMsg(3, 'A1', 'm1'),
    ev(4, 'turn/end', { reason: { kind: 'completed' } }),
    ev(5, 'turn/start', { turn: 2 }),
    userMsg(6, 'Q2'),
    assistantMsg(7, 'A2', 'm2'),
    ev(8, 'turn/end', { reason: { kind: 'completed' } }),
    ev(9, 'user/message',
      { role: 'user', content: [{ type: 'text', text: 'checkpoint' }] },
      { op: 'replace', startSeq: 1, endSeq: 4 }),
    ev(10, 'compaction/end', { compactionId: 'cpt', turn: 2 }),
    ev(11, 'turn/start', { turn: 3 }),
    userMsg(12, 'Q3'),
    assistantMsg(13, 'A3', 'm3'),
    ev(14, 'turn/end', { reason: { kind: 'completed' } }),
  ]
  const { ctx, created } = leafCtxBy({ 'dsh-a': compacted })
  const { map } = fakeMap()

  const res = leafRes()
  await sessionFork(ctx, map,
    forkReq({ source_dsh_id: 'dsh-a', seq: 14, new_lore_id: 'lore-branch-2' }), res)
  assert.equal(res.status, 200)
  assert.deepEqual(created[0].seed, forkSeed(compacted, 14),
    'rows 0..14 in seq order — the checkpoint row and its replace window ride verbatim')
  assert.equal(created[0].inheritedEventCount, 15)
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
  // off its own body. The turn drive lives in turn.ts since the step-4 split.
  const src = readFileSync(new URL('../src/turn.ts', import.meta.url), 'utf8')
  assert.match(src,
    /const effort = typeof body\.reasoning_effort === 'string' && body\.reasoning_effort\s+\? body\.reasoning_effort\s+: null/,
    'the effort is read beside the model (null = Default)')
  assert.match(src, /piEffort = piAiEffortKey\(effort\)/,
    'the gateway spelling is translated to the pi-ai level key')
  assert.match(src, /\.\.\.\(piEffort \? \{ reasoningEffort: ReasoningEffortId\(piEffort\) \} : \{\}\)/,
    'the translated effort rides the selection ONLY when present (absent key = Default)')
  assert.match(src, /installModelSelection\(agentCtx, selection\)/,
    'the turn setup installs the selection')
  assert.match(src, /const provider = ROUTE_BY_API\[caps\.api\]/,
    'the route name comes from the caps api — one map, two routes')
  assert.match(src, /const agentOptions = \{ provider, model \}/,
    'agentOptions keeps its {provider, model} runtime shape, provider off the map')
  assert.equal(src.match(/installModelSelection\(/g)?.length, 1,
    'exactly one install site — the fork create half stays selection-free')
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
  // Pinned by source shape, like the leaf/effort tests above. The handlers
  // live in turn.ts since the step-4 split.
  const src = readFileSync(new URL('../src/turn.ts', import.meta.url), 'utf8')
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
  // every mapped frame (the halt mint included) — and the replay carries it
  // for a session with no registered turn when this push died with the socket
  // (the backend's own mints stay on the breach and setup-failure paths). The
  // followup task lives in turn.ts since the step-4 split.
  const src = readFileSync(new URL('../src/turn.ts', import.meta.url), 'utf8')
  const finallyIdx = src.indexOf('} finally {\n      await finish()')
  const push = src.match(/try \{ channel\.push\(dshId, \{ type: 'turn_closed' \}\) \} catch/)
  assert.ok(finallyIdx !== -1, 'the followup task has a finally that finishes the turn')
  assert.ok(push, 'the turn_closed push exists, best-effort (a closed channel is the replay\'s to cover)')
  assert.ok(push!.index !== undefined && push!.index > finallyIdx,
    'turn_closed is pushed in the followup task\'s finally, after finish()')
})

// ─── /session-fork: the session-per-branch primitive (plan ───────────────────
// chat-branch-sessions). A branch is its OWN dsh session whose id EQUALS the
// Lore session id: the fork seeds under the CALLER-CHOSEN new_lore_id through
// dsh's own buildForkSeed (seedForkSessionCreate) and touches NO map entry
// and NO live subscription — nothing
// is re-pointed, sessions only ever append. Idempotent: an existing
// new_lore_id answers 200 without re-seeding (a crash between the fork and
// the backend clearing the row's seed_source_* makes the next turn call
// again; re-seeding would fork a second log under the same id).

test('a fork under a NEW id seeds without touching the map or the subscription', async () => {
  // leafCtxBy (not leafCtx): the existence probe OPENS new_lore_id — the
  // one-log fake would answer "exists" and never seed.
  const { ctx, created } = leafCtxBy({ 'dsh-a': log() })
  const { sets, map } = fakeMap() // lore-1 → dsh-a
  const { repoints, channel } = fakeChannel()

  const res = leafRes()
  await sessionFork(ctx, map,
    forkReq({ source_dsh_id: 'dsh-a', seq: 5, new_lore_id: 'lore-branch-2' }), res)
  assert.equal(res.status, 200)
  assert.deepEqual(JSON.parse(res.body), { ok: true })
  assert.equal(created.length, 1, 'the branch log was seeded')
  assert.equal(String(created[0].sessionId), 'lore-branch-2',
    'seeded under the CALLER-CHOSEN id — dsh id = Lore id')
  assert.equal(String(created[0].meta.parentSession), 'dsh-a',
    'the lineage names the source log')
  assert.deepEqual(created[0].seed, forkSeed(log(), 5),
    'the seed is the same buildForkSeed rows [0..5] + the inherited-cut marker')
  assert.equal(created[0].inheritedEventCount, 6)
  assert.deepEqual(sets, [], 'the map is untouched — no entry for the branch')
  assert.deepEqual(repoints, [], 'no live subscription moves — nothing re-pointed')
})

test('/session-fork is idempotent: an existing new_lore_id answers 200 without re-seeding', async () => {
  const { ctx, created, opened } = leafCtxBy({ 'dsh-a': log(), 'lore-branch-2': log() })
  const { sets, map } = fakeMap()
  const { repoints, channel } = fakeChannel()

  const res = leafRes()
  await sessionFork(ctx, map,
    forkReq({ source_dsh_id: 'dsh-a', seq: 5, new_lore_id: 'lore-branch-2' }), res)
  assert.equal(res.status, 200)
  assert.match(res.body, /"existed":\s*true/, 'the answer says the log already exists')
  assert.equal(created.length, 0, 'no re-seed under the same id')
  assert.deepEqual(opened, ['lore-branch-2'],
    'only the existence probe ran — the source log was not even read')
  assert.deepEqual(sets, [])
  assert.deepEqual(repoints, [])
})

test('/session-fork refuses a non-boundary seq and a missing source log (422, never a seed)', async () => {
  const { ctx, created } = leafCtxBy({ 'dsh-a': log() })
  const { map } = fakeMap()
  const { channel } = fakeChannel()

  const notBoundary = leafRes()
  await sessionFork(ctx, map,
    forkReq({ source_dsh_id: 'dsh-a', seq: 4, new_lore_id: 'lore-branch-2' }), notBoundary)
  assert.equal(notBoundary.status, 422, 'seq 4 is an assistant/message row, not a boundary')

  const missingSource = leafRes()
  await sessionFork(ctx, map,
    forkReq({ source_dsh_id: 'dsh-gone', seq: 5, new_lore_id: 'lore-branch-2' }), missingSource)
  assert.equal(missingSource.status, 422, 'a missing source log is unresolvable, not a 500')

  assert.equal(created.length, 0, 'an unresolvable branch point never seeds')
  void channel
})

test('/session-fork maps a legacy source id through SessionMap.get', async () => {
  // seed_source_session falls back to the chat's bare lore id when the
  // stamped row predates the (seq, session) pair — SessionMap.get resolves
  // it to the chat's CURRENT dsh id (identity default for ~f spellings).
  const { ctx, created, opened } = leafCtxBy({ 'dsh-a': log() })
  const { map } = fakeMap() // get: lore-1 → dsh-a
  const { channel } = fakeChannel()

  const res = leafRes()
  await sessionFork(ctx, map,
    forkReq({ source_dsh_id: 'lore-1', seq: 5, new_lore_id: 'lore-branch-2' }), res)
  assert.equal(res.status, 200)
  assert.deepEqual(opened, ['lore-branch-2', 'dsh-a'],
    'the existence probe, then the legacy id resolved to the current dsh id')
  assert.equal(String(created[0].meta.parentSession), 'dsh-a')
  void channel
})

/** A POST /session-fork request (same req shape the leaf tests use). */
function forkReq(body: unknown): any {
  const req = leafReq(body)
  req.url = '/session-fork'
  return req
}

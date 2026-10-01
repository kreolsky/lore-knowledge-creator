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

import { lastTurnEndSeq } from '../src/leaf.ts'
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

// index.ts captures the driver secret at MODULE LOAD (const SECRET), so the
// leaf-handler drive below needs it set BEFORE that module evaluates — which
// a static import cannot do (imports hoist above any env assignment). The
// dynamic import runs after both env guards; every other test file keeps
// plain imports because only index.ts reads this secret.
process.env.LORE_DRIVER_SECRET ||= 't3st-s3cr3t'
const {
  AGENT_KEY_REF, contextUsageFrame, ensureAgentKey, ensureTitleConfig,
  ensureTitleEntry, ensureWebSearch, forkModel, loreRoute, PROVIDER,
  sessionLeaf, turnConfigProblem,
} = await import('../src/index.ts')
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
  // else. 0.2.0 shape: llm-pi-ai mounts DORMANT in the composition (its
  // `providers` field is volatile, and a `!!js` expression inside the
  // volatile subtree breaks every settings write — the route cannot live in
  // the patch layer), and the plugin's boot/turn upsert declares the `lore`
  // route beside the model entries (loreRoute + ensureModelEntry in index.ts).
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
  // The route FACTS live in index.ts now — pinned by source shape, like the
  // provider-route test below: apiKeyEnv (the Lore-internal credentials
  // ref), the openai-completions api, the deepseek-mirroring compat trio
  // (role system, max_tokens, bare reasoning_effort), no fallback (no gateway
  // URL skips the scaffold and the turn is refused).
  const index = readFileSync(new URL('../src/index.ts', import.meta.url), 'utf8')
  assert.match(index, /apiKeyEnv: AGENT_KEY_REF,\n    api: 'openai-completions',\n    baseURL,/,
    'loreRoute carries the route facts the dormant row cannot')
  assert.match(index, /supportsDeveloperRole: false,\n      maxTokensField: 'max_tokens',\n      thinkingFormat: 'openai',/,
    'compat mirrors what the old route sent and the gateway accepts')
  assert.match(index, /if \(!baseURL\) return null/,
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
  for (const field of ['targetWords: 5', 'targetCjkCharacters: 10', 'maxInputBytes: 4096', 'maxOutputTokens: 64', 'timeoutMs: 60000']) {
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

test('a turn with no model is refused by name — there is no env fallback', () => {
  // The model comes only from the turn: the backend resolves body.model →
  // session row → admin CHAT_MODEL and refuses a model-less turn itself
  // (completions.py), so the harness's named refusal is the second gate, and
  // index.ts must carry no LORE_HARNESS_MODEL read at all — wiring, not
  // configuration (plan component-wiring-not-settings step 3).
  const index = readFileSync(new URL('../src/index.ts', import.meta.url), 'utf8')
  assert.ok(!index.includes('LORE_HARNESS_MODEL'),
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
  const index = readFileSync(new URL('../src/index.ts', import.meta.url), 'utf8')
  assert.ok(!index.includes('LORE_HARNESS_CONTEXT_WINDOW'),
    'no env window read remains in the plugin')
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
  maxOutputTokens: 64, timeoutMs: 60000,
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
  await ensureTitleConfig(editor, '')
  assert.deepEqual(editor.edits.at(-1)!.next, TITLE_FIXED,
    'an empty title_model removes the pair — the titler rides the session\'s own route')
  await ensureTitleConfig(editor, '')
  assert.equal(editor.edits.length, 2, 'removing an absent pair edits nothing')
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

const PLAIN = { known: true, vision: false, contextWindow: null, maxOutputTokens: null, effortLevels: [] }

test('the title model is catalogued once; a reasoner is still catalogued, with a warning', async () => {
  // pi-ai refuses an id outside the catalog, and the composition already
  // points the titler at the model — so a reasoner is warned about, never
  // left uncatalogued (titles would fail) and never an exit.
  const route = { apiKeyEnv: 'LORE_AGENT_API_KEY', api: 'openai-completions', baseURL: 'http://gw/v1', compat: {} }
  const settings = fakeCatalogSettings()
  await ensureTitleEntry(settings, 'titler/plain', PLAIN, route)
  await ensureTitleEntry(settings, 'titler/plain', PLAIN, route)
  assert.equal(settings.updates.length, 1, 'the committed entry writes nothing')
  const logged: string[] = []
  const saved = console.error
  console.error = (...args: unknown[]) => { logged.push(args.join(' ')) }
  try {
    await ensureTitleEntry(settings, 'titler/thinks', { ...PLAIN, effortLevels: ['low'] }, route)
    await ensureTitleEntry(settings, 'titler/thinks', { ...PLAIN, effortLevels: ['low'] }, route)
  } finally {
    console.error = saved
  }
  assert.equal(settings.updates.length, 2)
  assert.equal(logged.filter((l) => l.includes('advertises reasoning effort levels')).length, 1,
    'one loud line per model, not one per turn')
})

// ─── the fork model: the parent's last requested route ────────────────────────

test('a fork seeds with the model the parent last requested at the branch point', () => {
  const header = (seq: number, model: string) =>
    ev(seq, 'request/header', { header: { config: { provider: 'lore', model } } })
  const events = [header(0, 'm/first'), ev(1, 'turn/end'), header(2, 'm/second'), ev(3, 'turn/end')]
  assert.equal(forkModel(events, 1), 'm/first', 'a header past the branch point is not the parent\'s')
  assert.equal(forkModel(events, 3), 'm/second')
  assert.equal(forkModel([ev(0, 'turn/end')], 0), '',
    'no header → no fallback (the value every fresh install already forks with; the next turn selects its own model)')
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


// ─── /session-leaf takes the dsh seq: one id space — the branch point is a
// dsh log seq, never a turn ordinal.

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
  const baselines = createSessionStreamBaselines()

  // Real fork: branch after turn 1 (seq 5 < the live tail 9).
  const forkRes = leafRes()
  await sessionLeaf(ctx, map, channel, baselines, leafReq({ session_id: 'lore-1', seq: 5 }), forkRes)
  assert.equal(forkRes.status, 200)
  assert.equal(created.length, 1, 'a fresh session was seeded')
  assert.deepEqual(created[0].seed, forkSeed(log(), 5),
    'the seed is dsh’s buildForkSeed: rows [0..5] plus one inherited-cut marker')
  assert.equal(created[0].inheritedEventCount, 6,
    'the inherited cut is the boundary seq + 1 (dsh SessionController.fork’s shape)')
  assert.equal(sets.length, 1, 'the map repointed')
  const [loreId, from, to, tail] = repoints[0]
  assert.equal(loreId, 'lore-1')
  assert.equal(from, 'dsh-a', 'repointed FROM the pre-fork dsh id')
  assert.equal(to, sets[0][1], 'repointed TO the id the map took')
  assert.match(to, /^lore-1~f/, 'the fresh id is the fork spelling')
  assert.equal(tail, 5, 'the dedup anchor is the boundary seq, not the marker’s')

  // Live-tail no-op (seq = the live tail 9): no seed, no map.set, no repoint.
  const noopRes = leafRes()
  await sessionLeaf(ctx, map, channel, baselines, leafReq({ session_id: 'lore-1', seq: 9 }), noopRes)
  assert.equal(noopRes.status, 200)
  assert.equal(created.length, 1, 'no second seed on the live-tail no-op')
  assert.equal(sets.length, 1)
  assert.equal(repoints.length, 1, 'the live-tail no-op does NOT repoint')
})

test('the root fork repoints with a null tail', async () => {
  const { ctx } = leafCtx(log())
  const { sets, map } = fakeMap()
  const { repoints, channel } = fakeChannel()
  const baselines = createSessionStreamBaselines()
  baselines.observe('dsh-a', 4)
  baselines.accept('dsh-a', { type: 'start', attemptId: 'a1', revision: 1, turn: 2, step: 0 })
  assert.ok(baselines.snapshot('dsh-a')?.activeAttempt, 'the pre-fork id holds a live fold')

  const res = leafRes()
  await sessionLeaf(ctx, map, channel, baselines, leafReq({ session_id: 'lore-1', seq: null }), res)
  assert.equal(res.status, 200)
  assert.equal(sets.length, 1, 'the map repointed')
  assert.deepEqual(repoints[0], ['lore-1', 'dsh-a', sets[0][1], null],
    'root fork: repoint with tail null (the fresh log is empty)')
  assert.equal(baselines.snapshot('dsh-a'), undefined,
    'the root fork forgets the displaced id\'s fold too (same map move)')
})

test('a fork forgets the displaced id\'s stream fold; the live-tail no-op keeps it', async () => {
  // The map's move orphans the pre-fork id's fold: nothing reads it (a
  // snapshot keys by the CURRENT dsh id) and dsh's own consumer deletes a
  // fold on agent/disposed, which a forked-away id never receives — without
  // an explicit forget one fold leaks per fork for the process's lifetime.
  // The live-tail no-op is the counter-case: the session keeps streaming, so
  // its fold must survive (that path returns before any fork).
  const { ctx } = leafCtx(log()) // live tail: turn/end seq 9
  const { sets, map } = fakeMap()
  const { channel } = fakeChannel()
  const baselines = createSessionStreamBaselines()
  const seedFold = (): void => {
    baselines.observe('dsh-a', 4)
    baselines.accept('dsh-a', { type: 'start', attemptId: 'a1', revision: 1, turn: 2, step: 0 })
    baselines.accept('dsh-a', {
      type: 'chunk', attemptId: 'a1', revision: 2, index: 0, time: 5,
      chunk: { type: 'text-delta', index: 0, text: 'hi' },
    })
  }

  seedFold()
  assert.ok(baselines.snapshot('dsh-a')?.activeAttempt, 'the pre-fork id holds a live fold')
  const forkRes = leafRes()
  await sessionLeaf(ctx, map, channel, baselines, leafReq({ session_id: 'lore-1', seq: 5 }), forkRes)
  assert.equal(forkRes.status, 200)
  assert.equal(baselines.snapshot('dsh-a'), undefined,
    'the displaced id\'s fold is gone')
  assert.equal(baselines.snapshot(sets[0][1]), undefined,
    'the fresh id carries no fold (it never streamed)')

  // The live-tail no-op: the fold of the STILL-CURRENT session survives.
  seedFold()
  const noopRes = leafRes()
  await sessionLeaf(ctx, map, channel, baselines, leafReq({ session_id: 'lore-1', seq: 9 }), noopRes)
  assert.equal(noopRes.status, 200)
  assert.ok(baselines.snapshot('dsh-a')?.activeAttempt,
    'the live-tail no-op never drops the active fold')
})

test('a seq naming a non-turn/end row is refused (422), and so is a seq past the log', async () => {
  const { ctx, created } = leafCtx(log())
  const { map } = fakeMap()
  const { repoints, channel } = fakeChannel()
  const baselines = createSessionStreamBaselines()

  const notBoundary = leafRes()
  await sessionLeaf(ctx, map, channel, baselines, leafReq({ session_id: 'lore-1', seq: 4 }), notBoundary)
  assert.equal(notBoundary.status, 422, 'seq 4 is an assistant/message row, not a forkable boundary')

  const pastLog = leafRes()
  await sessionLeaf(ctx, map, channel, baselines, leafReq({ session_id: 'lore-1', seq: 99 }), pastLog)
  assert.equal(pastLog.status, 422, 'a seq beyond the last row never resolves')

  assert.equal(created.length, 0, 'an unresolvable branch point never seeds')
  assert.equal(repoints.length, 0)
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
  await sessionLeaf(ctx, map, channel, createSessionStreamBaselines(),
    leafReq({ session_id: 'lore-1', seq: 5, source: 'lore-1' }), res)
  assert.equal(res.status, 200)
  assert.deepEqual(opened, ['lore-1'], 'only the source log is read')
  assert.equal(created.length, 1)
  assert.deepEqual(created[0].seed, forkSeed(log(), 5),
    "the seed is the SOURCE's prefix through its seq-5 turn/end, plus the marker")
  assert.equal(String(created[0].meta.parentSession), 'lore-1', 'the fork descends from the source')
  assert.equal(created[0].inheritedEventCount, 6)
  assert.deepEqual(repoints[0], ['lore-1', 'dsh-a', sets[0][1], 5],
    'the live subscription still moves FROM the current session')
})

test('a seq without source resolves against current and 422s when absent there', async () => {
  const { ctx, created, opened } = leafCtxBy({ 'dsh-a': rootForkedLog(), 'lore-1': log() })
  const { sets, map } = fakeMap()
  const { repoints, channel } = fakeChannel()

  const res = leafRes()
  await sessionLeaf(ctx, map, channel, createSessionStreamBaselines(), leafReq({ session_id: 'lore-1', seq: 9 }), res)
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
  await sessionLeaf(ctx, map, channel, createSessionStreamBaselines(),
    leafReq({ session_id: 'lore-1', seq: 6, source: 'lore-1~fold0001' }), res)
  assert.equal(res.status, 200)
  assert.equal(created.length, 1, 'a real fork, not the live-tail no-op')
  assert.deepEqual(created[0].seed, forkSeed(abandoned, 6))
  assert.equal(sets.length, 1)
  assert.equal(repoints.length, 1)
})

test('source equal to the current session keeps the live-tail no-op', async () => {
  const { ctx, created } = leafCtxBy({ 'lore-1~fcur0001': rootForkedLog() })
  const { sets } = fakeMap()
  const map: any = { get: () => 'lore-1~fcur0001', set: (l: string, d: string) => { sets.push([l, d]) } }
  const { repoints, channel } = fakeChannel()

  const res = leafRes()
  await sessionLeaf(ctx, map, channel, createSessionStreamBaselines(),
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
    await sessionLeaf(ctx, map, channel, createSessionStreamBaselines(),
      leafReq({ session_id: 'lore-1', seq: 5, source }), res)
    assert.equal(res.status, 422, `source ${JSON.stringify(source)} must be refused`)
  }
  assert.deepEqual(opened, [], "a foreign source's log is never opened")

  const missing = leafRes()
  await sessionLeaf(ctx, map, channel, createSessionStreamBaselines(),
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
  const { ctx, created } = leafCtxBy({ 'dsh-a': rootForkedLog(), 'lore-1~fcmp0001': compacted })
  const { map } = fakeMap()
  const { channel } = fakeChannel()

  const res = leafRes()
  await sessionLeaf(ctx, map, channel, createSessionStreamBaselines(),
    leafReq({ session_id: 'lore-1', seq: 14, source: 'lore-1~fcmp0001' }), res)
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

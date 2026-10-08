/**
 * Tests for caps.ts — the driver's model-capability resolution (plan
 * collapse-the-editor-harness-layer step 4): the gateway /v1/models roster is
 * the ONE source; the deleted Python twins (resolve_context_window /
 * resolve_max_output_tokens / model_supports_vision) asserted this same
 * parsing and now live only here. The /v1/capabilities reasoning map rides
 * the same resolution (plan harness-effort-levels-via-pi-ai): levels in
 * GATEWAY spellings, the pi-ai translation table, and the title-model gate.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  gatewayOf,
  piAiEffortKey,
  reasoningEffortsDeclaration,
  resolveModelCaps,
  resetCapsCache,
} from '../src/caps.ts'

/** URL-aware fake: /models serves the roster, /capabilities the reasoning map. */
function fakeFetch(entries: unknown[], caps: unknown = {}, log: string[] = []) {
  return (async (url: unknown, _init?: unknown) => {
    log.push(String(url))
    if (String(url).endsWith('/capabilities')) {
      return { ok: true, status: 200, json: async () => caps }
    }
    return { ok: true, status: 200, json: async () => ({ data: entries }) }
  }) as unknown as typeof fetch
}

function brokenFetch(log: string[] = []) {
  return (async (url: unknown, _init?: unknown) => {
    log.push(String(url))
    throw new Error('gateway down')
  }) as unknown as typeof fetch
}

const SERVED = [
  {
    id: 'deepseek/flash', supports_vision: false,
    architecture: { input_modalities: ['text'] },
    context_length: 524288, max_completion_tokens: 131072,
  },
  {
    // The router's anthropic-protocol entry:
    // `api` names the wire protocol, `compat` rides verbatim.
    id: 'deepseek-a/flash', api: 'anthropic-messages',
    context_length: 524288, max_completion_tokens: 131072,
  },
  {
    id: 'local/orange/chat', supports_vision: true,
    architecture: { input_modalities: ['text', 'image'] },
    context_length: 131072, max_completion_tokens: 32768,
  },
  // Degraded variants the deleted Python tests pinned too.
  { id: 'gemini/chat' }, // omits everything — known, non-vision, no numbers
  { id: 'm/modalities', architecture: { input_modalities: ['text', 'image'] } },
  { id: 'm/string-window', context_length: '262144' },
  { id: 'm/garbage-levels' }, // served, so its /capabilities entry is read
  { id: 'm/weird-api', api: 'carrier-pigeon' }, // a protocol we do not speak
]

test('a served model resolves its numbers and vision off the roster', async () => {
  resetCapsCache()
  const caps = await resolveModelCaps('deepseek/flash', { fetch: fakeFetch(SERVED) })
  assert.deepEqual(caps, {
    known: true, vision: false, contextWindow: 524288, maxOutputTokens: 131072,
    api: 'openai-completions', compat: null,
    effortLevels: null,
  }, 'no /capabilities entry for the model → null levels (unknown)')
  const vision = await resolveModelCaps('local/orange/chat', { fetch: fakeFetch(SERVED) })
  assert.equal(vision.vision, true)
})

test('vision falls back to input_modalities; a bare entry is NOT vision', async () => {
  resetCapsCache()
  const io = { fetch: fakeFetch(SERVED) }
  assert.equal((await resolveModelCaps('m/modalities', io)).vision, true,
    'losing `supports_vision` must not silently disable image delivery')
  assert.equal((await resolveModelCaps('gemini/chat', io)).vision, false,
    'unknown capability strips images (the safe direction), never a 400')
})

test('a numeric-string context_length parses; garbage numbers stay null', async () => {
  resetCapsCache()
  const caps = await resolveModelCaps('m/string-window', { fetch: fakeFetch(SERVED) })
  assert.equal(caps.contextWindow, 262144)
  const bare = await resolveModelCaps('gemini/chat', { fetch: fakeFetch(SERVED) })
  assert.equal(bare.contextWindow, null)
  assert.equal(bare.maxOutputTokens, null,
    'no floor guess: the omitted ceiling is the named residue (dsh default)')
})

test('an unserved id answers UNKNOWN (named defaults, never a guess)', async () => {
  resetCapsCache()
  const caps = await resolveModelCaps('never-served/model', { fetch: fakeFetch(SERVED) })
  assert.deepEqual(caps, {
    known: false, vision: false, contextWindow: null, maxOutputTokens: null,
    api: 'openai-completions', compat: null,
    effortLevels: null,
  })
})

test('an unreadable gateway degrades to UNKNOWN, never a thrown gate', async () => {
  resetCapsCache()
  const caps = await resolveModelCaps('local/orange/chat', { fetch: brokenFetch() })
  assert.deepEqual(caps, {
    known: false, vision: false, contextWindow: null, maxOutputTokens: null,
    api: 'openai-completions', compat: null,
    effortLevels: null,
  })
})

// ─── the roster api + compat: which route a model rides ──────────────────────
// The router's /v1/models `api` field picks the
// pi-ai route name; its `compat` rides the catalog entry verbatim (the router
// owns the allow-list — dsh refuses a withheld key loudly, naming it).

test('the roster api names the wire protocol; absent or unknown stays openai-completions', async () => {
  resetCapsCache()
  const io = { fetch: fakeFetch(SERVED) }
  assert.equal((await resolveModelCaps('deepseek-a/flash', io)).api,
    'anthropic-messages', 'the entry names the Messages protocol')
  assert.equal((await resolveModelCaps('deepseek/flash', io)).api,
    'openai-completions', 'an entry without api keeps today\'s one route')
  assert.equal((await resolveModelCaps('m/weird-api', io)).api,
    'openai-completions', 'a protocol Lore does not route degrades to the openai route, never a guess')
  assert.equal((await resolveModelCaps('never-served/model', io)).api,
    'openai-completions', 'an unserved id answers the openai route (UNKNOWN)')
})

test('the entry compat rides verbatim — null when absent, no key filtering here', async () => {
  resetCapsCache()
  const roster = [
    {
      id: 'deepseek-a/flash', api: 'anthropic-messages',
      compat: { forceAdaptiveThinking: true, supportsTemperature: false },
      context_length: 524288, max_completion_tokens: 131072,
    },
    { id: 'claude/opus', api: 'anthropic-messages' },
    { id: 'm/compat-list', compat: ['not', 'an', 'object'] },
  ]
  const io = { fetch: fakeFetch(roster) }
  assert.deepEqual((await resolveModelCaps('deepseek-a/flash', io)).compat,
    { forceAdaptiveThinking: true, supportsTemperature: false },
    'the plugin does NOT filter compat keys: the router owns the allow-list')
  assert.equal((await resolveModelCaps('claude/opus', io)).compat, null,
    'absent compat is null, not {} — null means "write no compat key"')
  assert.equal((await resolveModelCaps('m/compat-list', io)).compat, null,
    'a non-object compat value is absence, never a failed turn')
})

// ─── the /capabilities reasoning map ─────────────────────────────────────────

test('effort levels ride the capabilities map in GATEWAY spellings', async () => {
  resetCapsCache()
  const caps = {
    'deepseek/flash': { supported: true, effort_levels: ['low', 'high', 'max'] },
    'local/orange/chat': { supported: false, effort_levels: [] },
    'm/garbage-levels': { supported: true, effort_levels: ['low', 7, null] },
    'garbage-entry': 'not-a-dict',
  }
  const io = { fetch: fakeFetch(SERVED, caps) }
  assert.deepEqual(
    (await resolveModelCaps('deepseek/flash', io)).effortLevels,
    ['low', 'high', 'max'])
  // supported=false is the gateway's own word: definitely non-reasoning ([]),
  // not unknown (null) — the upsert DROPS any committed declaration for it.
  assert.deepEqual(
    (await resolveModelCaps('local/orange/chat', io)).effortLevels,
    [])
  // Non-string levels are dropped, the rest survive; a garbage entry is
  // feature absence (same softness as the backend picker twin).
  assert.deepEqual(
    (await resolveModelCaps('m/garbage-levels', io)).effortLevels,
    ['low'])
  // A model ABSENT from the map answers null (unknown), even when served.
  assert.equal(
    (await resolveModelCaps('gemini/chat', io)).effortLevels,
    null)
})

test('a /capabilities failure keeps the roster numbers and nulls only the levels', async () => {
  resetCapsCache()
  const fetch = (async (url: unknown, _init?: unknown) => {
    if (String(url).endsWith('/capabilities')) {
      return { ok: false, status: 404, json: async () => ({}) }
    }
    return { ok: true, status: 200, json: async () => ({ data: SERVED }) }
  }) as unknown as typeof fetch
  const caps = await resolveModelCaps('deepseek/flash', { fetch })
  assert.equal(caps.known, true, 'the roster half stands')
  assert.equal(caps.contextWindow, 524288)
  assert.equal(caps.effortLevels, null, 'feature absence, never a failed turn')
})

test('the roster is TTL-cached: one fetch per endpoint serves every model within the window', async () => {
  resetCapsCache()
  const log: string[] = []
  const fetch = fakeFetch(SERVED, {}, log)
  let now = 1_000
  const io = { fetch, now: () => now }
  await resolveModelCaps('deepseek/flash', io)
  await resolveModelCaps('local/orange/chat', io)
  assert.equal(log.length, 2, 'both reads share ONE /models and ONE /capabilities fetch')
  now += 61_000
  await resolveModelCaps('deepseek/flash', io)
  assert.equal(log.length, 4, 'past the TTL the next read refetches both endpoints')
})

test('concurrent cold reads share one in-flight fetch per endpoint', async () => {
  resetCapsCache()
  const log: string[] = []
  const io = { fetch: fakeFetch(SERVED, {}, log) }
  await Promise.all([
    resolveModelCaps('deepseek/flash', io),
    resolveModelCaps('local/orange/chat', io),
  ])
  assert.equal(log.length, 2)
})

test('the cache is keyed by the gateway: an admin change of URL or key refetches at once', async () => {
  resetCapsCache()
  const calls: { url: string; auth: string }[] = []
  const fetch = (async (url: unknown, init?: { headers?: Record<string, string> }) => {
    calls.push({ url: String(url), auth: init?.headers?.Authorization ?? '' })
    if (String(url).endsWith('/capabilities')) return { ok: true, status: 200, json: async () => ({}) }
    return { ok: true, status: 200, json: async () => ({ data: SERVED }) }
  }) as unknown as typeof fetch
  const now = () => 1_000
  await resolveModelCaps('deepseek/flash', { fetch, now, gateway: { base: 'http://old/v1', key: 'k1' } })
  await resolveModelCaps('deepseek/flash', { fetch, now, gateway: { base: 'http://old/v1', key: 'k1' } })
  assert.equal(calls.length, 2, 'the same gateway inside the TTL is served from the cache')
  await resolveModelCaps('deepseek/flash', { fetch, now, gateway: { base: 'http://new/v1', key: 'k1' } })
  await resolveModelCaps('deepseek/flash', { fetch, now, gateway: { base: 'http://new/v1', key: 'k2' } })
  assert.deepEqual(calls.slice(2).map((c) => [c.url, c.auth]), [
    ['http://new/v1/models', 'Bearer k1'], ['http://new/v1/capabilities', 'Bearer k1'],
    ['http://new/v1/models', 'Bearer k2'], ['http://new/v1/capabilities', 'Bearer k2'],
  ])
})

test('gatewayOf: the request values win; the harness env is only the fallback', () => {
  const saved = { url: process.env.LORE_AI_API_URL, key: process.env.AI_API_KEY }
  try {
    process.env.LORE_AI_API_URL = 'http://env/v1'
    process.env.AI_API_KEY = 'env-key'
    assert.deepEqual(gatewayOf('http://admin/v1/', 'admin-key'), { base: 'http://admin/v1', key: 'admin-key' })
    assert.deepEqual(gatewayOf('', undefined), { base: 'http://env/v1', key: 'env-key' })
    delete process.env.LORE_AI_API_URL
    delete process.env.AI_API_KEY
    assert.deepEqual(gatewayOf(undefined, ''), { base: '', key: '' })
  } finally {
    for (const [name, value] of [['LORE_AI_API_URL', saved.url], ['AI_API_KEY', saved.key]] as const) {
      if (value === undefined) delete process.env[name]
      else process.env[name] = value
    }
  }
})

// ─── the pi-ai level translation (the adapter twin of the backend filter) ─────

test('piAiEffortKey: identity for pi-ai names, none→off, unknown null', () => {
  for (const level of ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']) {
    assert.equal(piAiEffortKey(level), level, `${level} is its own pi-ai key`)
  }
  assert.equal(piAiEffortKey('none'), 'off', 'the gateway synonym maps onto off')
  assert.equal(piAiEffortKey('ultra'), null, 'an unnameable spelling answers null')
  assert.equal(piAiEffortKey(''), null)
})

test('reasoningEffortsDeclaration: identity levels pass through verbatim', () => {
  assert.deepEqual(
    reasoningEffortsDeclaration(['low', 'medium', 'xhigh'], 'm/x'),
    { low: 'low', medium: 'medium', xhigh: 'xhigh' })
})

test('reasoningEffortsDeclaration: none becomes off with the gateway wire spelling', () => {
  // luna's offer — Off is selectable, the wire carries the gateway's own word.
  assert.deepEqual(
    reasoningEffortsDeclaration(['none', 'low', 'high'], 'openai/luna'),
    { off: 'none', low: 'low', high: 'high' })
})

test('reasoningEffortsDeclaration: an unnameable level is dropped and logged once', () => {
  const errors: string[] = []
  const original = console.error
  console.error = (line: unknown) => { errors.push(String(line)) }
  try {
    assert.deepEqual(
      reasoningEffortsDeclaration(['low', 'ultra', 'mega'], 'm/x'),
      { low: 'low' })
  } finally {
    console.error = original
  }
  assert.equal(errors.length, 1, 'one line per model, not per level')
  assert.match(errors[0]!, /"ultra", "mega" of m\/x has no pi-ai key — not offered/)
})

test('reasoningEffortsDeclaration: empty (or all-unnameable) offers are undefined', () => {
  assert.equal(reasoningEffortsDeclaration([], 'm/x'), undefined,
    'an entry without the field does not reason')
  assert.equal(reasoningEffortsDeclaration(['ultra'], 'm/x'), undefined)
})

test('reasoningEffortsDeclaration: an offer naming nothing beyond off declares nothing', () => {
  // pi-ai refuses an off-only declaration (catalog.ts:688) and a failed
  // catalog resolution takes the WHOLE provider route out (adapter.ts:135) —
  // so an off-only offer declares nothing and the model runs non-reasoning;
  // its turns die one-model-loud, never route-wide.
  const errors: string[] = []
  const original = console.error
  console.error = (line: unknown) => { errors.push(String(line)) }
  try {
    assert.equal(reasoningEffortsDeclaration(['none'], 'm/none-only'), undefined,
      'the none synonym alone is no offer')
    assert.equal(reasoningEffortsDeclaration(['off'], 'm/off-literal'), undefined,
      'a literal off alone is no offer either')
    assert.equal(reasoningEffortsDeclaration(['none', 'ultra'], 'm/mixed'), undefined,
      'an unnameable level does not rescue the off-only offer')
    assert.deepEqual(reasoningEffortsDeclaration(['none', 'low'], 'm/ok'),
      { off: 'none', low: 'low' },
      'off rides along when a level beyond it is nameable')
  } finally {
    console.error = original
  }
  assert.equal(errors.length, 3, 'one line per refused offer')
  assert.match(errors[0]!, /names no pi-ai level beyond off.*runs non-reasoning/)
  assert.match(errors[2]!, /"ultra".*unnameable/, 'the off-only line names unnameable levels')
})


import test from 'node:test'
import assert from 'node:assert/strict'

import { WebError } from '@deepseek-ai/dsh-web'
import type { WebSearchProvider } from '@deepseek-ai/dsh-web'

import { BraveSearchProvider } from '../src/web-search/brave.ts'
import { PerplexitySearchProvider } from '../src/web-search/perplexity.ts'
import { RouterSearchProvider } from '../src/web-search/router.ts'
import { SearxngSearchProvider } from '../src/web-search/searxng.ts'
import { TavilySearchProvider } from '../src/web-search/tavily.ts'
import { WEB_SEARCH_KEY_REF } from '../src/web-search/key.ts'

interface Call { url: string, init: RequestInit | undefined }

/** A fetch that records its calls and answers with one canned response. */
function fakeFetch(status: number, body: unknown): { calls: Call[], fetchImpl: (u: string, i?: RequestInit) => Promise<Response> } {
  const calls: Call[] = []
  return {
    calls,
    fetchImpl: async (url, init) => {
      calls.push({ url, init })
      const text = typeof body === 'string' ? body : JSON.stringify(body)
      return new Response(text, { status, headers: { 'content-type': 'application/json' } })
    },
  }
}

/** The live credential store the resolvers read: a key set AFTER apply is
 * used by the very next call — an admin change reaches the next search. */
function keyStore(initial = '') {
  const store = new Map<string, string>([[WEB_SEARCH_KEY_REF, initial]])
  return {
    store,
    resolveKey: async (): Promise<string> => store.get(WEB_SEARCH_KEY_REF) ?? '',
  }
}

interface Case {
  name: string
  make: (resolveKey: () => Promise<string>, fetchImpl?: (u: string, i?: RequestInit) => Promise<Response>) => WebSearchProvider
  config: string
  /** The provider's wire shape for: one full row, one url-less row, one sparse row. */
  payload: unknown
  /** A successful answer with no rows. */
  empty: unknown
}

const ROUTER_GATEWAY = async () => ({ base: 'http://router:8080/v1', key: 'router-key' })

/** An Anthropic Messages answer: the server tool's result block plus `extra` blocks. */
function messages(results: unknown, ...extra: unknown[]): unknown {
  return { content: [
    { type: 'server_tool_use', id: 'srv_1', name: 'web_search', input: { query: 'q' } },
    { type: 'web_search_tool_result', tool_use_id: 'srv_1', content: results },
    ...extra,
  ] }
}

const CASES: Case[] = [
  {
    name: 'brave',
    make: (resolveKey, f) => new BraveSearchProvider(resolveKey, f),
    config: 'brave-key',
    payload: { web: { results: [
      { title: 'Full <strong>row</strong>', url: 'https://a.example/1', description: 'The <strong>snippet</strong>', page_age: '2026-09-01T00:00:00' },
      { title: 'No url', description: 'dropped' },
      { url: 'https://a.example/2', title: '', description: null },
    ] } },
    empty: { web: { results: [] } },
  },
  {
    name: 'tavily',
    make: (resolveKey, f) => new TavilySearchProvider(resolveKey, f),
    config: 'tvly-key',
    payload: { results: [
      { title: 'Full row', url: 'https://a.example/1', content: 'The snippet', published_date: '2026-09-01T00:00:00' },
      { title: 'No url', content: 'dropped' },
      { url: 'https://a.example/2', title: '', content: null },
    ] },
    empty: { results: [] },
  },
  {
    name: 'searxng',
    make: (resolveKey, f) => new SearxngSearchProvider(resolveKey, f),
    config: 'http://searxng:8080/',
    payload: { results: [
      { title: 'Full row', url: 'https://a.example/1', content: 'The snippet', publishedDate: '2026-09-01T00:00:00' },
      { title: 'No url', content: 'dropped' },
      { url: 'https://a.example/2', title: '', content: null },
    ] },
    empty: { results: [] },
  },
  {
    name: 'perplexity',
    make: (resolveKey, f) => new PerplexitySearchProvider('fast', resolveKey, f),
    config: 'pplx-key',
    payload: { id: 'r1', results: [
      { title: 'Full row', url: 'https://a.example/1', snippet: 'The snippet', date: '2026-09-01T00:00:00', last_updated: '2026-09-30' },
      { title: 'No url', snippet: 'dropped' },
      { url: 'https://a.example/2', title: '', snippet: null, date: null },
    ] },
    empty: { results: [] },
  },
  {
    name: 'router',
    make: (resolveKey, f) => new RouterSearchProvider(resolveKey, ROUTER_GATEWAY, f),
    config: 'claude/haiku',
    payload: messages([
      { type: 'web_search_result', url: 'https://a.example/1', title: 'Full row', page_age: '2026-09-01T00:00:00', encrypted_content: 'x' },
      { type: 'web_search_result', title: 'No url', encrypted_content: 'x' },
      { type: 'web_search_result', url: 'https://a.example/2', title: '', page_age: null },
    ], { type: 'text', text: 'Found.', citations: [
      { type: 'web_search_result_location', url: 'https://a.example/1', title: 'Full row', cited_text: 'The snippet' },
    ] }),
    empty: messages([]),
  },
]

const ADMIN_PATH = 'Admin panel → Search → Web search'

for (const c of CASES) {
  test(`${c.name}: maps its wire shape to sources and drops url-less rows`, async () => {
    const { fetchImpl } = fakeFetch(200, c.payload)
    const result = await c.make(keyStore(c.config).resolveKey, fetchImpl).search({ query: 'q', maxResults: 5 })
    assert.deepEqual(result, {
      truncated: false,
      sources: [
        { url: 'https://a.example/1', title: 'Full row', snippet: 'The snippet', publishedAt: '2026-09-01T00:00:00' },
        { url: 'https://a.example/2' },
      ],
    })
  })

  test(`${c.name}: available() is true with an EMPTY key — the empty key fails inside search()`, async () => {
    // The no-off shape dsh DeepSeek already has: a pinned provider is always
    // usable, so dsh never silently falls back to another provider; the empty
    // key is the loud call-time error that names the admin path.
    const provider = c.make(keyStore('').resolveKey)
    assert.equal(provider.available(), true)
    await assert.rejects(
      provider.search({ query: 'q' }),
      (error: unknown) => {
        assert.ok(error instanceof WebError)
        assert.equal(error.code, 'WEB_PROVIDER_CREDENTIAL_MISSING')
        assert.match(error.message, /Admin panel → Search → Web search/, 'the fix names the admin path')
        return true
      },
    )
  })

  test(`${c.name}: a key set after apply is used by the next call`, async () => {
    const keys = keyStore('')
    const { calls, fetchImpl } = fakeFetch(200, c.empty)
    keys.store.set(WEB_SEARCH_KEY_REF, c.config)
    await c.make(keys.resolveKey, fetchImpl).search({ query: 'q' })
    assert.equal(calls.length, 1, 'the rotated key reached the wire')
  })

  test(`${c.name}: a non-2xx answer throws WEB_PROVIDER_ERROR with the status and a bounded excerpt`, async () => {
    const { fetchImpl } = fakeFetch(429, `rate limited ${'x'.repeat(500)}`)
    await assert.rejects(
      c.make(keyStore(c.config).resolveKey, fetchImpl).search({ query: 'q' }),
      (error: unknown) => {
        assert.ok(error instanceof WebError)
        assert.equal(error.code, 'WEB_PROVIDER_ERROR')
        assert.match(error.message, /HTTP 429/)
        assert.match(error.message, /rate limited/)
        assert.ok(error.message.length < 300, error.message)
        return true
      },
    )
  })

  test(`${c.name}: an abort surfaces as WEB_ABORTED`, async () => {
    const fetchImpl = async () => { throw new DOMException('aborted', 'AbortError') }
    await assert.rejects(
      c.make(keyStore(c.config).resolveKey, fetchImpl).search({ query: 'q' }),
      (error: unknown) => error instanceof WebError && error.code === 'WEB_ABORTED',
    )
  })
}

test('the empty-key error names the provider id and the SearXNG URL spelling', async () => {
  // One probe per id: the operator reads the error to know WHICH provider to
  // key, and SearXNG's credential is a URL, not a key.
  const brave = await new BraveSearchProvider(keyStore('').resolveKey).search({ query: 'q' }).catch((e: unknown) => String((e as Error).message))
  assert.match(brave, /lore-brave: no API key/)
  const tavily = await new TavilySearchProvider(keyStore('').resolveKey).search({ query: 'q' }).catch((e: unknown) => String((e as Error).message))
  assert.match(tavily, /lore-tavily: no API key/)
  const searxng = await new SearxngSearchProvider(keyStore('').resolveKey).search({ query: 'q' }).catch((e: unknown) => String((e as Error).message))
  assert.match(searxng, /lore-searxng: no API key \(URL for SearXNG\)/)
  for (const mode of ['fast', 'web'] as const) {
    const perplexity = await new PerplexitySearchProvider(mode, keyStore('').resolveKey).search({ query: 'q' }).catch((e: unknown) => String((e as Error).message))
    assert.match(perplexity, new RegExp(`lore-perplexity-${mode}: no API key`))
  }
})

test('brave: sends the key header, the query and a count capped at 20', async () => {
  const { calls, fetchImpl } = fakeFetch(200, { web: { results: [] } })
  await new BraveSearchProvider(keyStore('brave-key').resolveKey, fetchImpl).search({ query: 'лор мир', maxResults: 50 })
  const url = new URL(calls[0].url)
  assert.equal(url.origin + url.pathname, 'https://api.search.brave.com/res/v1/web/search')
  assert.equal(url.searchParams.get('q'), 'лор мир')
  assert.equal(url.searchParams.get('count'), '20')
  assert.equal((calls[0].init?.headers as Record<string, string>)['x-subscription-token'], 'brave-key')
})

test('tavily: POSTs the query and max_results with a bearer key', async () => {
  const { calls, fetchImpl } = fakeFetch(200, { results: [] })
  await new TavilySearchProvider(keyStore('tvly-key').resolveKey, fetchImpl).search({ query: 'q', maxResults: 8 })
  assert.equal(calls[0].url, 'https://api.tavily.com/search')
  assert.equal(calls[0].init?.method, 'POST')
  assert.equal((calls[0].init?.headers as Record<string, string>).authorization, 'Bearer tvly-key')
  assert.deepEqual(JSON.parse(calls[0].init?.body as string), { query: 'q', max_results: 8 })
})

test('perplexity: each mode is its own pinnable id and POSTs its search_type with max_results capped at 20', async () => {
  for (const mode of ['fast', 'web'] as const) {
    const { calls, fetchImpl } = fakeFetch(200, { results: [] })
    const provider = new PerplexitySearchProvider(mode, keyStore('pplx-key').resolveKey, fetchImpl)
    assert.equal(provider.id, `lore-perplexity-${mode}`)
    await provider.search({ query: 'q', maxResults: 50 })
    assert.equal(calls[0].url, 'https://api.perplexity.ai/search')
    assert.equal(calls[0].init?.method, 'POST')
    assert.equal((calls[0].init?.headers as Record<string, string>).authorization, 'Bearer pplx-key')
    assert.deepEqual(JSON.parse(calls[0].init?.body as string), { query: 'q', search_type: mode, max_results: 20 })
  }
})

test('perplexity: a row without a publication date carries last_updated', async () => {
  const { fetchImpl } = fakeFetch(200, { results: [{ url: 'https://a.example/1', title: 't', snippet: 's', date: null, last_updated: '2026-09-30' }] })
  const result = await new PerplexitySearchProvider('fast', keyStore('k').resolveKey, fetchImpl).search({ query: 'q' })
  assert.equal(result.sources[0].publishedAt, '2026-09-30')
})

test('searxng: GETs {base}/search with format=json, trailing slash folded', async () => {
  const { calls, fetchImpl } = fakeFetch(200, { results: [] })
  await new SearxngSearchProvider(keyStore('http://searxng:8080/').resolveKey, fetchImpl).search({ query: 'q', maxResults: 8 })
  assert.equal(calls[0].url, 'http://searxng:8080/search?q=q&format=json')
})

test('searxng: a non-URL base is the named empty-key-class error, not a fetch failure', async () => {
  await assert.rejects(
    new SearxngSearchProvider(keyStore('not a url').resolveKey).search({ query: 'q' }),
    (error: unknown) => error instanceof WebError
      && error.code === 'WEB_PROVIDER_CREDENTIAL_MISSING'
      && /lore-searxng/.test(error.message),
  )
})

// ─── the router: an Anthropic Messages call through Lore's AI router ────────

test('router: POSTs {AI_API_URL}/messages with the router key as x-api-key, the model and a one-use web_search tool', async () => {
  const { calls, fetchImpl } = fakeFetch(200, messages([]))
  await new RouterSearchProvider(keyStore('claude/haiku').resolveKey, ROUTER_GATEWAY, fetchImpl).search({ query: 'surrealdb release' })
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, 'http://router:8080/v1/messages')
  const headers = calls[0].init?.headers as Record<string, string>
  assert.equal(headers['x-api-key'], 'router-key')
  assert.equal(headers['anthropic-version'], '2023-06-01')
  assert.ok(!Object.keys(headers).some(h => h.toLowerCase() === 'authorization'), 'no Authorization header')
  const body = JSON.parse(String(calls[0].init?.body))
  assert.equal(body.model, 'claude/haiku')
  assert.deepEqual(body.tools, [{ type: 'web_search_20250305', name: 'web_search', max_uses: 1 }])
  assert.match(JSON.stringify(body.messages), /surrealdb release/)
})

test('router: a duplicated url across results is kept once', async () => {
  const row = { type: 'web_search_result', url: 'https://a.example/1', title: 'One' }
  const { fetchImpl } = fakeFetch(200, messages([row, row]))
  const result = await new RouterSearchProvider(keyStore('m').resolveKey, ROUTER_GATEWAY, fetchImpl).search({ query: 'q' })
  assert.deepEqual(result.sources, [{ url: 'https://a.example/1', title: 'One' }])
})

test('router: a search error reported with HTTP 200 is WEB_PROVIDER_ERROR naming the error_code', async () => {
  const { fetchImpl } = fakeFetch(200, messages({ type: 'web_search_tool_result_error', error_code: 'too_many_requests' }))
  await assert.rejects(
    new RouterSearchProvider(keyStore('m').resolveKey, ROUTER_GATEWAY, fetchImpl).search({ query: 'q' }),
    (error: unknown) => error instanceof WebError && error.code === 'WEB_PROVIDER_ERROR'
      && /lore-router/.test(error.message) && /too_many_requests/.test(error.message),
  )
})

test('router: an answer without a search block is WEB_PROVIDER_ERROR, never an empty success', async () => {
  const { fetchImpl } = fakeFetch(200, { content: [{ type: 'text', text: 'I know this already.' }] })
  await assert.rejects(
    new RouterSearchProvider(keyStore('m').resolveKey, ROUTER_GATEWAY, fetchImpl).search({ query: 'q' }),
    (error: unknown) => error instanceof WebError && error.code === 'WEB_PROVIDER_ERROR'
      && /did not run a web search/.test(error.message),
  )
})

test('router: an Anthropic-shaped 401 carries the status and the error type', async () => {
  const { fetchImpl } = fakeFetch(401, { type: 'error', error: { type: 'authentication_error', message: 'invalid x-api-key' } })
  await assert.rejects(
    new RouterSearchProvider(keyStore('m').resolveKey, ROUTER_GATEWAY, fetchImpl).search({ query: 'q' }),
    (error: unknown) => error instanceof WebError && /lore-router/.test(error.message)
      && /HTTP 401/.test(error.message) && /authentication_error/.test(error.message),
  )
})

test('router: an empty model names the model fix; an empty gateway URL or key names the AI API fix', async () => {
  const { calls, fetchImpl } = fakeFetch(200, messages([]))
  await assert.rejects(
    new RouterSearchProvider(keyStore('').resolveKey, ROUTER_GATEWAY, fetchImpl).search({ query: 'q' }),
    /lore-router: no model/)
  for (const gateway of [{ base: '', key: 'k' }, { base: 'http://router:8080/v1', key: '' }]) {
    await assert.rejects(
      new RouterSearchProvider(keyStore('m').resolveKey, async () => gateway, fetchImpl).search({ query: 'q' }),
      (error: unknown) => error instanceof WebError && error.code === 'WEB_PROVIDER_CREDENTIAL_MISSING'
        && /lore-router: no AI API URL\/key/.test(error.message) && /Admin panel → Models → AI API/.test(error.message),
    )
  }
  assert.equal(calls.length, 0, 'nothing reaches the wire without a model and a gateway')
})

// ─── the plugin: registration over the live credentials service ─────────────

test('apply before the credentials service is composed: the next search still resolves its key', async () => {
  const { apply } = await import('../src/web-search/index.ts')
  let credentials: { resolve: (ref: string) => Promise<{ value: string } | undefined> } | undefined
  const providers: WebSearchProvider[] = []
  apply({
    get: (name: string) => (name === 'credentials' ? credentials : undefined),
    web: { registerSearchProvider: (p: WebSearchProvider) => { providers.push(p) } },
  } as never)
  // The service arrives after apply — the boot order that broke every Lore provider.
  credentials = { resolve: async () => undefined }
  const brave = providers.find(p => p.id === 'lore-brave')
  assert.ok(brave)
  await assert.rejects(
    brave.search({ query: 'q' }),
    (error: unknown) => error instanceof WebError && error.code === 'WEB_PROVIDER_CREDENTIAL_MISSING',
    'the key lookup reached the late service (empty key), not "not composed"',
  )
})

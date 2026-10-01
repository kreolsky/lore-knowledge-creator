import test from 'node:test'
import assert from 'node:assert/strict'

import { WebError } from '@deepseek-ai/dsh-web'
import type { WebSearchProvider } from '@deepseek-ai/dsh-web'

import { BraveSearchProvider } from '../src/web-search/brave.ts'
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
    const { calls, fetchImpl } = fakeFetch(200, c.name === 'brave' ? { web: { results: [] } } : { results: [] })
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

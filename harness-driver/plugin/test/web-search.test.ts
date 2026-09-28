import test from 'node:test'
import assert from 'node:assert/strict'

import { WebError } from '@deepseek-ai/dsh-web'
import type { WebSearchProvider } from '@deepseek-ai/dsh-web'

import { BraveSearchProvider } from '../src/web-search/brave.ts'
import { SearxngSearchProvider } from '../src/web-search/searxng.ts'
import { TavilySearchProvider } from '../src/web-search/tavily.ts'

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

interface Case {
  name: string
  make: (config: string, fetchImpl?: (u: string, i?: RequestInit) => Promise<Response>) => WebSearchProvider
  config: string
  /** The provider's wire shape for: one full row, one url-less row, one sparse row. */
  payload: unknown
}

const CASES: Case[] = [
  {
    name: 'brave',
    make: (key, f) => new BraveSearchProvider(key, f),
    config: 'brave-key',
    payload: { web: { results: [
      { title: 'Full <strong>row</strong>', url: 'https://a.example/1', description: 'The <strong>snippet</strong>', page_age: '2026-09-01T00:00:00' },
      { title: 'No url', description: 'dropped' },
      { url: 'https://a.example/2', title: '', description: null },
    ] } },
  },
  {
    name: 'tavily',
    make: (key, f) => new TavilySearchProvider(key, f),
    config: 'tvly-key',
    payload: { results: [
      { title: 'Full row', url: 'https://a.example/1', content: 'The snippet', published_date: '2026-09-01T00:00:00' },
      { title: 'No url', content: 'dropped' },
      { url: 'https://a.example/2', title: '', content: null },
    ] },
  },
  {
    name: 'searxng',
    make: (base, f) => new SearxngSearchProvider(base, f),
    config: 'http://searxng:8080/',
    payload: { results: [
      { title: 'Full row', url: 'https://a.example/1', content: 'The snippet', publishedDate: '2026-09-01T00:00:00' },
      { title: 'No url', content: 'dropped' },
      { url: 'https://a.example/2', title: '', content: null },
    ] },
  },
]

for (const c of CASES) {
  test(`${c.name}: maps its wire shape to sources and drops url-less rows`, async () => {
    const { fetchImpl } = fakeFetch(200, c.payload)
    const result = await c.make(c.config, fetchImpl).search({ query: 'q', maxResults: 5 })
    assert.deepEqual(result, {
      truncated: false,
      sources: [
        { url: 'https://a.example/1', title: 'Full row', snippet: 'The snippet', publishedAt: '2026-09-01T00:00:00' },
        { url: 'https://a.example/2' },
      ],
    })
  })

  test(`${c.name}: available() is false on an empty config and true on a set one`, () => {
    assert.equal(c.make('').available(), false)
    assert.equal(c.make(c.config).available(), true)
  })

  test(`${c.name}: a non-2xx answer throws WEB_PROVIDER_ERROR with the status and a bounded excerpt`, async () => {
    const { fetchImpl } = fakeFetch(429, `rate limited ${'x'.repeat(500)}`)
    await assert.rejects(
      c.make(c.config, fetchImpl).search({ query: 'q' }),
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
      c.make(c.config, fetchImpl).search({ query: 'q' }),
      (error: unknown) => error instanceof WebError && error.code === 'WEB_ABORTED',
    )
  })
}

test('brave: sends the key header, the query and a count capped at 20', async () => {
  const { calls, fetchImpl } = fakeFetch(200, { web: { results: [] } })
  await new BraveSearchProvider('brave-key', fetchImpl).search({ query: 'лор мир', maxResults: 50 })
  const url = new URL(calls[0].url)
  assert.equal(url.origin + url.pathname, 'https://api.search.brave.com/res/v1/web/search')
  assert.equal(url.searchParams.get('q'), 'лор мир')
  assert.equal(url.searchParams.get('count'), '20')
  assert.equal((calls[0].init?.headers as Record<string, string>)['x-subscription-token'], 'brave-key')
})

test('tavily: POSTs the query and max_results with a bearer key', async () => {
  const { calls, fetchImpl } = fakeFetch(200, { results: [] })
  await new TavilySearchProvider('tvly-key', fetchImpl).search({ query: 'q', maxResults: 8 })
  assert.equal(calls[0].url, 'https://api.tavily.com/search')
  assert.equal(calls[0].init?.method, 'POST')
  assert.equal((calls[0].init?.headers as Record<string, string>).authorization, 'Bearer tvly-key')
  assert.deepEqual(JSON.parse(calls[0].init?.body as string), { query: 'q', max_results: 8 })
})

test('searxng: GETs {base}/search with format=json, trailing slash folded', async () => {
  const { calls, fetchImpl } = fakeFetch(200, { results: [] })
  await new SearxngSearchProvider('http://searxng:8080/', fetchImpl).search({ query: 'q', maxResults: 8 })
  assert.equal(calls[0].url, 'http://searxng:8080/search?q=q&format=json')
})

test('searxng: a non-URL base is unavailable', () => {
  assert.equal(new SearxngSearchProvider('not a url').available(), false)
})

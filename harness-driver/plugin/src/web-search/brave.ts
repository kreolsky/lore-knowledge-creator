/** Brave Search API provider: `GET /res/v1/web/search`, key in `X-Subscription-Token`. */

import type { WebSearchProvider, WebSearchRequest, WebSearchResult } from '@deepseek-ai/dsh-web'

import { requestJson, source, toResult, type FetchLike } from './http.ts'

export const BRAVE_PROVIDER_ID = 'lore-brave'
const ENDPOINT = 'https://api.search.brave.com/res/v1/web/search'
/** Brave refuses a `count` above 20. */
const MAX_COUNT = 20

interface BraveResult { url?: unknown, title?: unknown, description?: unknown, page_age?: unknown }

/** Brave marks query hits in `description` with inline HTML (`<strong>`). */
function stripTags(value: unknown): unknown {
  return typeof value === 'string' ? value.replace(/<[^>]*>/g, '') : value
}

export function mapBraveResponse(payload: unknown): WebSearchResult {
  return toResult((payload as { web?: { results?: BraveResult[] } } | null)?.web?.results ?? [], r => source(r.url, stripTags(r.title), stripTags(r.description), r.page_age))
}

export class BraveSearchProvider implements WebSearchProvider {
  readonly id = BRAVE_PROVIDER_ID

  constructor(private readonly apiKey: string, private readonly fetchImpl: FetchLike = fetch) {}

  available(): boolean {
    return this.apiKey.length > 0
  }

  async search(request: WebSearchRequest, signal?: AbortSignal): Promise<WebSearchResult> {
    const params = new URLSearchParams({ q: request.query })
    if (request.maxResults !== undefined) params.set('count', String(Math.min(request.maxResults, MAX_COUNT)))
    const payload = await requestJson(this.fetchImpl, 'Brave', `${ENDPOINT}?${params}`, {
      headers: { 'accept': 'application/json', 'x-subscription-token': this.apiKey },
    }, signal)
    return mapBraveResponse(payload)
  }
}

/** Tavily provider: `POST /search`, key as a bearer token. */

import type { WebSearchProvider, WebSearchRequest, WebSearchResult } from '@deepseek-ai/dsh-web'

import { requestJson, source, toResult, type FetchLike } from './http.ts'

export const TAVILY_PROVIDER_ID = 'lore-tavily'
const ENDPOINT = 'https://api.tavily.com/search'
/** Tavily refuses a `max_results` above 20. */
const MAX_RESULTS = 20

interface TavilyResult { url?: unknown, title?: unknown, content?: unknown, published_date?: unknown }

export function mapTavilyResponse(payload: unknown): WebSearchResult {
  return toResult((payload as { results?: TavilyResult[] } | null)?.results ?? [], r => source(r.url, r.title, r.content, r.published_date))
}

export class TavilySearchProvider implements WebSearchProvider {
  readonly id = TAVILY_PROVIDER_ID

  constructor(private readonly apiKey: string, private readonly fetchImpl: FetchLike = fetch) {}

  available(): boolean {
    return this.apiKey.length > 0
  }

  async search(request: WebSearchRequest, signal?: AbortSignal): Promise<WebSearchResult> {
    const payload = await requestJson(this.fetchImpl, 'Tavily', ENDPOINT, {
      method: 'POST',
      headers: {
        'accept': 'application/json',
        'authorization': `Bearer ${this.apiKey}`,
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        query: request.query,
        ...request.maxResults !== undefined ? { max_results: Math.min(request.maxResults, MAX_RESULTS) } : {},
      }),
    }, signal)
    return mapTavilyResponse(payload)
  }
}

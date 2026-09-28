/** SearXNG provider: `GET {base}/search?format=json` on an instance with the JSON format enabled. */

import type { WebSearchProvider, WebSearchRequest, WebSearchResult } from '@deepseek-ai/dsh-web'

import { requestJson, source, toResult, type FetchLike } from './http.ts'

export const SEARXNG_PROVIDER_ID = 'lore-searxng'

interface SearxngResult { url?: unknown, title?: unknown, content?: unknown, publishedDate?: unknown }

export function mapSearxngResponse(payload: unknown): WebSearchResult {
  return toResult((payload as { results?: SearxngResult[] } | null)?.results ?? [], r => source(r.url, r.title, r.content, r.publishedDate))
}

export class SearxngSearchProvider implements WebSearchProvider {
  readonly id = SEARXNG_PROVIDER_ID
  private readonly baseUrl: string

  constructor(baseUrl: string, private readonly fetchImpl: FetchLike = fetch) {
    this.baseUrl = baseUrl.replace(/\/+$/, '')
  }

  available(): boolean {
    return this.baseUrl.length > 0 && URL.canParse(this.baseUrl)
  }

  // WHY no result-count parameter: SearXNG's JSON API has none; the web seam
  // truncates to `maxResults` on the way back.
  async search(request: WebSearchRequest, signal?: AbortSignal): Promise<WebSearchResult> {
    const params = new URLSearchParams({ q: request.query, format: 'json' })
    const payload = await requestJson(this.fetchImpl, 'SearXNG', `${this.baseUrl}/search?${params}`, {
      headers: { accept: 'application/json' },
    }, signal)
    return mapSearxngResponse(payload)
  }
}

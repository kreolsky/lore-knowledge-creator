/** SearXNG provider: `GET {base}/search?format=json` on an instance with the JSON format enabled. */

import type { WebSearchProvider, WebSearchRequest, WebSearchResult } from '@deepseek-ai/dsh-web'

import { missingCredential, requestJson, source, toResult, type FetchLike } from './http.ts'

export const SEARXNG_PROVIDER_ID = 'lore-searxng'

interface SearxngResult { url?: unknown, title?: unknown, content?: unknown, publishedDate?: unknown }

export function mapSearxngResponse(payload: unknown): WebSearchResult {
  return toResult((payload as { results?: SearxngResult[] } | null)?.results ?? [], r => source(r.url, r.title, r.content, r.publishedDate))
}

/**
 * The base URL is the provider's CREDENTIAL: it resolves at CALL time from
 * the credentials service (the per-turn apply writes it), and an empty or
 * non-URL base fails inside `search()` naming this provider and the admin
 * path — `available()` stays true (the no-off shape), so dsh never falls back
 * to another provider.
 */
export class SearxngSearchProvider implements WebSearchProvider {
  readonly id = SEARXNG_PROVIDER_ID

  constructor(
    private readonly resolveBase: () => Promise<string>,
    private readonly fetchImpl: FetchLike = fetch,
  ) {}

  available(): boolean {
    return true
  }

  // WHY no result-count parameter: SearXNG's JSON API has none; the web seam
  // truncates to `maxResults` on the way back.
  async search(request: WebSearchRequest, signal?: AbortSignal): Promise<WebSearchResult> {
    const base = (await this.resolveBase()).replace(/\/+$/, '')
    if (base === '' || !URL.canParse(base)) throw missingCredential(this.id, true)
    const params = new URLSearchParams({ q: request.query, format: 'json' })
    const payload = await requestJson(this.fetchImpl, 'SearXNG', `${base}/search?${params}`, {
      headers: { accept: 'application/json' },
    }, signal)
    return mapSearxngResponse(payload)
  }
}

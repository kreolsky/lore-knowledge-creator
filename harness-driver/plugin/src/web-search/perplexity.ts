/** Perplexity Search API provider: `POST /search` (raw ranked results, no Sonar model), key as a bearer token. */

import type { WebSearchProvider, WebSearchRequest, WebSearchResult } from '@deepseek-ai/dsh-web'

import { missingCredential, requestJson, source, toResult, type FetchLike } from './http.ts'

/** The Search API's `search_type` values Lore offers: `fast` is the cheaper, lower-latency tier. */
export type PerplexitySearchType = 'fast' | 'web'

/** One pinnable id per search type — the admin's mode choice rides the pin itself. */
export const PERPLEXITY_PROVIDER_IDS: Record<PerplexitySearchType, string> = {
  fast: 'lore-perplexity-fast',
  web: 'lore-perplexity-web',
}
const ENDPOINT = 'https://api.perplexity.ai/search'
/** Perplexity refuses a `max_results` above 20 for `web` and `fast`. */
const MAX_RESULTS = 20

interface PerplexityResult { url?: unknown, title?: unknown, snippet?: unknown, date?: unknown, last_updated?: unknown }

export function mapPerplexityResponse(payload: unknown): WebSearchResult {
  return toResult(
    (payload as { results?: PerplexityResult[] } | null)?.results ?? [],
    r => source(r.url, r.title, r.snippet, r.date ?? r.last_updated),
  )
}

/**
 * The key resolves at CALL time from the credentials service (the per-turn
 * apply writes it): an admin key change reaches the next search, and an empty
 * key fails inside `search()` naming this provider and the admin path —
 * `available()` stays true (the no-off shape), so dsh never falls back to
 * another provider.
 */
export class PerplexitySearchProvider implements WebSearchProvider {
  readonly id: string

  constructor(
    private readonly searchType: PerplexitySearchType,
    private readonly resolveKey: () => Promise<string>,
    private readonly fetchImpl: FetchLike = fetch,
  ) {
    this.id = PERPLEXITY_PROVIDER_IDS[searchType]
  }

  available(): boolean {
    return true
  }

  async search(request: WebSearchRequest, signal?: AbortSignal): Promise<WebSearchResult> {
    const apiKey = await this.resolveKey()
    if (apiKey === '') throw missingCredential(this.id, false)
    const payload = await requestJson(this.fetchImpl, 'Perplexity', ENDPOINT, {
      method: 'POST',
      headers: {
        'accept': 'application/json',
        'authorization': `Bearer ${apiKey}`,
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        query: request.query,
        search_type: this.searchType,
        ...request.maxResults !== undefined ? { max_results: Math.min(request.maxResults, MAX_RESULTS) } : {},
      }),
    }, signal)
    return mapPerplexityResponse(payload)
  }
}

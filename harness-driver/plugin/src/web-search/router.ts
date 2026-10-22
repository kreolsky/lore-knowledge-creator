/**
 * Web search through Lore's AI router: one Anthropic Messages call
 * (`POST {AI_API_URL}/messages`) with the native `web_search_20250305` server
 * tool, on a router model whose upstream runs server-side search.
 *
 * WHY through the router and not a direct call: the router already owns the
 * upstream key and the egress (a per-provider proxy where the install needs
 * one), and dsh's process-wide proxy refuses SOCKS — so a direct call can be
 * unreachable where the chat works. The router passes a Messages body through
 * untouched except the model rewrite.
 */

import { WebError } from '@deepseek-ai/dsh-web'
import type { WebSearchProvider, WebSearchRequest, WebSearchResult, WebSearchSource } from '@deepseek-ai/dsh-web'

import { requestJson, source, type FetchLike } from './http.ts'

export const ROUTER_PROVIDER_ID = 'lore-router'
const ANTHROPIC_VERSION = '2023-06-01'
const MAX_TOKENS = 1024

/** The router one search runs against: the turn's AI_API_URL (ends in /v1) and key. */
export interface RouterGateway { base: string, key: string }

interface Citation { url?: unknown, cited_text?: unknown }
interface ResultItem { type?: unknown, url?: unknown, title?: unknown, page_age?: unknown }
interface Block { type?: unknown, content?: unknown, citations?: Citation[] }

/**
 * `url → cited_text` from every `text` block's citations — result items carry
 * no snippet, the excerpt lives in the citing text (first occurrence wins).
 * WHY inlined: dsh's `citationSnippets` (web-search-deepseek/src/provider.ts)
 * is not on that package's main export, and this plugin declares no workspace
 * dependency to reach its `./src/*` subpath.
 */
function citationSnippets(blocks: readonly Block[]): Map<string, unknown> {
  const map = new Map<string, unknown>()
  for (const block of blocks) {
    if (block.type !== 'text') continue
    for (const cite of block.citations ?? []) {
      if (typeof cite.url === 'string' && cite.url && !map.has(cite.url)) map.set(cite.url, cite.cited_text)
    }
  }
  return map
}

/**
 * A Messages response → sources. Anthropic reports a failed search with HTTP
 * 200 and an error OBJECT as the result block's `content`, so that case is
 * named here instead of crashing on a non-array; no result block at all means
 * the model answered without searching — an error, never an empty success.
 */
export function mapRouterResponse(payload: unknown): WebSearchResult {
  const blocks = ((payload as { content?: unknown } | null)?.content ?? []) as Block[]
  const results = Array.isArray(blocks) ? blocks.filter(b => b.type === 'web_search_tool_result') : []
  if (results.length === 0) {
    throw new WebError(`${ROUTER_PROVIDER_ID}: the model did not run a web search`, 'WEB_PROVIDER_ERROR')
  }
  const snippets = citationSnippets(blocks)
  const seen = new Set<string>()
  const sources: WebSearchSource[] = []
  for (const block of results) {
    if (!Array.isArray(block.content)) {
      const code = (block.content as { error_code?: unknown } | undefined)?.error_code
      throw new WebError(`${ROUTER_PROVIDER_ID}: web search failed: ${String(code ?? 'unknown error')}`, 'WEB_PROVIDER_ERROR')
    }
    for (const item of block.content as ResultItem[]) {
      if (item.type !== 'web_search_result') continue
      const row = source(item.url, item.title, typeof item.url === 'string' ? snippets.get(item.url) : undefined, item.page_age)
      if (row === undefined || seen.has(row.url)) continue
      seen.add(row.url)
      sources.push(row)
    }
  }
  return { sources, truncated: false }
}

/**
 * The model and the gateway resolve at CALL time from the credentials service
 * (the per-turn apply writes them), so an admin change reaches the next
 * search; a missing piece fails inside `search()` naming this provider and
 * the admin path — `available()` stays true (the no-off shape).
 */
export class RouterSearchProvider implements WebSearchProvider {
  readonly id = ROUTER_PROVIDER_ID

  constructor(
    private readonly resolveModel: () => Promise<string>,
    private readonly resolveGateway: () => Promise<RouterGateway>,
    private readonly fetchImpl: FetchLike = fetch,
  ) {}

  available(): boolean {
    return true
  }

  async search(request: WebSearchRequest, signal?: AbortSignal): Promise<WebSearchResult> {
    const model = await this.resolveModel()
    if (model === '') {
      throw new WebError(
        `${ROUTER_PROVIDER_ID}: no model — set Web search model (or Default chat model) in Admin panel → Search → Web search`,
        'WEB_PROVIDER_CREDENTIAL_MISSING')
    }
    const gateway = await this.resolveGateway()
    if (gateway.base === '' || gateway.key === '') {
      throw new WebError(
        `${ROUTER_PROVIDER_ID}: no AI API URL/key — set them in Admin panel → Models → AI API`,
        'WEB_PROVIDER_CREDENTIAL_MISSING')
    }
    const payload = await requestJson(this.fetchImpl, ROUTER_PROVIDER_ID, `${gateway.base.replace(/\/+$/, '')}/messages`, {
      method: 'POST',
      headers: {
        'accept': 'application/json',
        'anthropic-version': ANTHROPIC_VERSION,
        'content-type': 'application/json',
        'x-api-key': gateway.key,
      },
      body: JSON.stringify({
        model,
        max_tokens: MAX_TOKENS,
        messages: [{ role: 'user', content: [{ type: 'text', text: `Perform a web search for the query: ${request.query}` }] }],
        tools: [{ type: 'web_search_20250305', name: 'web_search', max_uses: 1 }],
      }),
    }, signal)
    return mapRouterResponse(payload)
  }
}

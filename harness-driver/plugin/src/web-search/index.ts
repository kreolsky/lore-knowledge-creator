/**
 * Lore's web-search providers for dsh's `ctx.web` seam: Brave, Tavily, SearXNG,
 * Perplexity (one pinnable id per Search API `search_type`).
 *
 * # SYSTEM: web-search — the agent's one `web_search`, harness-served; provider picked in admin settings
 *
 * # ARCH: dsh owns search — provider selection by the pinned id (the `web`
 *   entry's `searchProvider`, edited per turn by the driver plugin), the
 *   result cap, cancellation, the `web_search` tool and its rendering. This
 *   plugin adds ONLY the backends dsh does not ship; DeepSeek search stays
 *   dsh's own `web-search-deepseek`. Every provider resolves its credential
 *   at CALL time from the credentials service (ref LORE_WEB_SEARCH_KEY — the
 *   per-turn apply writes it), so an admin change reaches the next search.
 *   An empty key/URL registers an ALWAYS-AVAILABLE provider that fails its
 *   calls loudly naming the provider and the admin path — web search has no
 *   off state, and a pinned provider never silently falls back to another.
 */

import type { Context } from '@deepseek-ai/cordis'

import { BraveSearchProvider } from './brave.ts'
import { WEB_SEARCH_KEY_REF } from './key.ts'
import { PerplexitySearchProvider } from './perplexity.ts'
import { SearxngSearchProvider } from './searxng.ts'
import { TavilySearchProvider } from './tavily.ts'

export const name = 'lore-web-search'
export const inject = ['web']

/** Register every Lore provider over the ONE per-turn credential ref. */
export function apply(ctx: Context): void {
  const credentials = ctx.get('credentials')
  const resolveKey = async (): Promise<string> => {
    if (credentials === undefined) {
      throw new Error(
        'the credentials service is not composed — cannot resolve '
        + WEB_SEARCH_KEY_REF)
    }
    return (await credentials.resolve(WEB_SEARCH_KEY_REF))?.value ?? ''
  }
  ctx.web.registerSearchProvider(new BraveSearchProvider(resolveKey))
  ctx.web.registerSearchProvider(new TavilySearchProvider(resolveKey))
  ctx.web.registerSearchProvider(new SearxngSearchProvider(resolveKey))
  ctx.web.registerSearchProvider(new PerplexitySearchProvider('fast', resolveKey))
  ctx.web.registerSearchProvider(new PerplexitySearchProvider('web', resolveKey))
}

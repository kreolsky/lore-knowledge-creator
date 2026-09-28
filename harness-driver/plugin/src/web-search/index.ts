/**
 * Lore's web-search providers for dsh's `ctx.web` seam: Brave, Tavily, SearXNG.
 *
 * # SYSTEM: web-search — the agent's one `web_search`, harness-served; provider picked in admin settings
 *
 * # ARCH: dsh owns search — provider selection by the pinned id
 *   (`$DSH_WEB_SEARCH_PROVIDER`), the result cap, cancellation, the
 *   `web_search` tool and its rendering. This plugin adds ONLY the backends dsh
 *   does not ship; DeepSeek search stays dsh's own `web-search-deepseek`. The
 *   pin and the credentials arrive in the env at boot (boot-env.ts, from
 *   Lore's admin settings), read here through the launch-environment snapshot
 *   like every dsh provider reads its key.
 */

import type { Context } from '@deepseek-ai/cordis'
import { launchEnvironmentOf } from '@deepseek-ai/dsh-launch-environment'
import type {} from '@deepseek-ai/dsh-web'

import { BraveSearchProvider } from './brave.ts'
import { SearxngSearchProvider } from './searxng.ts'
import { TavilySearchProvider } from './tavily.ts'

export const name = 'lore-web-search'
export const inject = ['web']

/**
 * Register all three providers. An empty key/URL registers an UNAVAILABLE
 * provider, never none: pinned, it fails every call with
 * WEB_PROVIDER_CONFIGURED_UNAVAILABLE instead of a silent fallback.
 */
export function apply(ctx: Context): void {
  const env = launchEnvironmentOf(ctx)
  const value = (key: string): string => env.get(key)?.value ?? ''
  ctx.web.registerSearchProvider(new BraveSearchProvider(value('BRAVE_API_KEY')))
  ctx.web.registerSearchProvider(new TavilySearchProvider(value('TAVILY_API_KEY')))
  ctx.web.registerSearchProvider(new SearxngSearchProvider(value('SEARXNG_URL')))
}

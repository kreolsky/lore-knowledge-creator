/**
 * The ONE Lore-internal credentials ref every web-search key resolves through
 * — written per turn by the driver plugin (`ensureWebSearch`), resolved at
 * call time by the Lore providers and (as `apiKeyEnv`) by dsh's own
 * web-search-deepseek row in the composition.
 *
 * WHY not the provider env names (DEEPSEEK_API_KEY, BRAVE_API_KEY, …):
 * credentials-local lets the inherited env win and refuses to write a ref it
 * holds, and prod's `.env` sets DEEPSEEK_API_KEY — the same reason
 * AGENT_KEY_REF exists in the driver plugin.
 */
export const WEB_SEARCH_KEY_REF = 'LORE_WEB_SEARCH_KEY'

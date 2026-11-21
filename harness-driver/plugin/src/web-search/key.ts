/**
 * The ONE Lore-internal credentials ref every web-search key resolves through
 * — written per turn by the driver plugin (`ensureWebSearch`), resolved at
 * call time by the Lore providers and (as `apiKeyEnv`) by dsh's own
 * web-search-deepseek row in the composition.
 *
 * WHY not the provider env names (DEEPSEEK_API_KEY, BRAVE_API_KEY, …):
 * a stored credential outranks the launch env and an unset falls back to it,
 * and prod's `.env` sets DEEPSEEK_API_KEY — the admin key would re-key every
 * other reader of that name, and a cleared one would resume the .env key. The
 * same reason AGENT_KEY_REF exists in the driver plugin.
 */
export const WEB_SEARCH_KEY_REF = 'LORE_WEB_SEARCH_KEY'

/**
 * The turn's AI_API_URL, for the router search provider — written per turn by
 * `ensureWebSearch`, resolved at call time (the gateway key already lives under
 * the driver plugin's AGENT_KEY_REF). A credentials ref, not module state:
 * the writer and the reader are two cordis plugins.
 */
export const ROUTER_URL_REF = 'LORE_ROUTER_URL'

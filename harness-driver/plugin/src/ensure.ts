/**
 * The per-turn configuration ensurers — everything a turn applies to the
 * composition BEFORE the agent runs: the model-catalog upsert
 * (ensureModelEntry), the agent key, the web-search pin, the title model.
 * index.ts re-exports the public surface.
 */

import { reasoningEffortsDeclaration } from './caps.ts'
import { WEB_SEARCH_KEY_REF } from './web-search/key.ts'

/** The LLM route an openai-completions model drives: the `lore` provider
 * route of the llm-pi-ai adapter, hand-declared (pi-ai's installed catalog
 * ships nothing under this key), as configured by loreRoute in turn.ts. An
 * anthropic-messages model rides the `anthropic` route instead — the pi-ai
 * INSTALLED-CATALOG provider id, which is why that name is load-bearing (the
 * catalog's protocol and compat inheritance come with it). Every model id the
 * gateway serves is upserted into its route's catalog by the plugin itself
 * (ensureModelEntry) — pi-ai refuses an id outside the catalog
 * (UNKNOWN_MODEL), which is what makes the upsert the seam it runs on. */
export const PROVIDER = 'lore'

// ── The per-turn image-capability upsert.
//
// # ARCH: pi-ai's request modalities default to [text] — a model entry
// without an `input` field accepts text only and the harness refuses an
// image before it is attached — so a turn carrying image blocks must declare
// its model image-capable in the `llm-pi-ai.providers.<route>` settings
// section (the entry's `input` field) — the ONLY seam; the adapter re-reads
// its profiles per request, so the entry reaches the very next call with no
// restart. The section ACCUMULATES entries keyed by model id (no cap, no
// eviction): a single-entry catalog would let a turn on model Y evict X's
// entry between X's write and X's dispatch and fail X with a refused image
// attachment.

const LLM_NS = 'llm-pi-ai'
/** The default provider route (the openai-completions half of ROUTE_BY_API in
 * turn.ts): the settings entry path is `providers.<route>`, and the
 * anthropic-messages half names its own route per model. */
const ROUTE = 'lore'

/** # INVARIANT: the `models` array is replaced WHOLESALE by a settings
 * update, so every read-compute-write upsert must run exclusively behind this
 * chain. Why: the settings service serializes writes per namespace but cannot
 * merge two stale reads — without the chain, two concurrent image turns on
 * different models each compute from the same snapshot and one entry is
 * silently lost. A lost entry is self-healing (the next turn on that model
 * upserts again), so this bounds a retry, not a corruption. */
let settingsWriteChain: Promise<unknown> = Promise.resolve()

/** One catalog entry as the `llm-pi-ai.providers.<route>` settings section
 * shapes it: pi-ai's field names (`input`, `reasoningEfforts`, `compat`),
 * not the old route's. */
interface CatalogModelEntry {
  id: string
  input?: readonly string[]
  contextWindow?: number
  maxTokens?: number
  reasoningEfforts?: Record<string, string>
  compat?: Record<string, unknown>
}

/** The provider-route scaffold declared beside the model entries: everything
 * the route needs that is NOT a model. The static fields are constants;
 * baseURL comes from the turn (gatewayOf), because the composition cannot
 * carry it (see cordis.patch.yml's llm-pi-ai note: `providers` is volatile,
 * and a `!!js` expression inside the volatile subtree breaks every settings
 * write).
 * `api` and `compat` are OPTIONAL and route-specific: the `lore` route
 * declares both (a hand-declared route must name its protocol), the
 * `anthropic` route declares NEITHER — the route name is the pi-ai catalog
 * provider id, so the catalog's shared protocol applies and compat is per
 * model, never a route fact. */
export interface LoreRouteScaffold {
  apiKeyEnv: string
  api?: string
  baseURL: string
  compat?: Record<string, unknown>
  defaultContextWindow?: number
}

/** The credentials ref the route's key resolves through — Lore-internal, set
 * by no .env and written only by ensureAgentKey.
 * WHY not AI_API_KEY: credentials-local lets the inherited env win and refuses
 * to write a ref it holds, and every compose loads .env into the harness, so
 * under AI_API_KEY an old .env key would forever shadow the admin key. */
export const AGENT_KEY_REF = 'LORE_AGENT_API_KEY'

/** The route's fallback context window for a model the gateway leaves bare
 * (caps.ts UNKNOWN): a constant, not configuration — the real window comes
 * from the gateway caps per model, and what connects Lore's own components
 * is not a knob anyone sets.
 * Same number the composes' deleted env default carried: pi-ai's own
 * fallback (262144) would silently change the context_usage denominator. */
export const ROUTE_DEFAULT_CONTEXT_WINDOW = 128000

/** The settings seam the upsert talks to (ctx.settings; duck-typed for tests).
 * 0.2.0 shape: no namespace `get` — `describe()` reads the live form values
 * (base config + profile patch merged), `update()` deep-merges a patch into
 * the entry's user section (plain-object merge, arrays REPLACE). */
export interface SettingsSeam {
  describe(): readonly { ns: unknown; value: unknown }[]
  update(ns: unknown, patch: Record<string, unknown>): Promise<void>
}

/** Equality for the committed-vs-candidate skip check, the efforts dict
 * included: both sides are built from JSON-shaped data with the same keys, so
 * a sorted-key JSON compare is exact and order-insensitive. */
function _sameEfforts(
  a: Record<string, string> | undefined,
  b: Record<string, string> | undefined,
): boolean {
  if (a === undefined || b === undefined) return a === b
  const sort = (o: Record<string, string>): string => JSON.stringify(Object.keys(o).sort().map((k) => [k, o[k]]))
  return sort(a) === sort(b)
}

/** The compat twin of _sameEfforts: sorted-key JSON, undefined-vs-present is a
 * difference. Without it a compat the router adds later would never land on a
 * committed entry (the skip check would swallow the only change). */
function _sameCompat(
  a: Record<string, unknown> | undefined,
  b: Record<string, unknown> | undefined,
): boolean {
  if (a === undefined || b === undefined) return a === b
  const sort = (o: Record<string, unknown>): string => JSON.stringify(Object.keys(o).sort().map((k) => [k, o[k]]))
  return sort(a) === sort(b)
}

/** Upsert the model's catalog entry into the `llm-pi-ai.providers.<route>`
 * settings section (`route` = the ROUTE name; `scaffold` = that route's
 * scaffold, loreRoute/anthropicRoute in turn.ts) — `{id, contextWindow,
 * maxTokens}` plus `input: ['text','image']` ONLY when the turn carried
 * images, `reasoningEfforts` (pi-ai key → gateway wire spelling) whenever the
 * gateway advertises levels for the model (null levels = no information: the
 * committed declaration rides along untouched), and `compat` verbatim
 * whenever the gateway's entry carries one (the router owns the allow-list;
 * null writes no compat key and rewrites a committed one away); skips the
 * write when the committed entry already carries exactly those values. Runs
 * on EVERY turn: the per-model `maxTokens` (the router's
 * `max_completion_tokens`, resolved by the driver itself — caps.ts) is the
 * output cap dsh honors ahead of the connection profile — without the entry
 * every model ran at one hardcoded number. `ifAbsent` (the title-entry path)
 * writes ONLY when no entry exists: a committed entry stays untouched
 * instead of being downgraded to id-only until the model's next turn.
 * `scaffold` rides the SAME write whenever the committed route section's
 * baseURL or apiKeyEnv differs from it: the scaffold is declared beside the
 * models because pi-ai refuses a provider-less/empty route — the composition
 * mounts llm-pi-ai dormant and THIS is the only writer — and the adapter
 * re-reads its profiles per request, so an admin change of the gateway
 * reaches the next request with no restart. A model the router moves between
 * protocols keeps its stale entry on the old route: removing it could empty
 * a route (pi-ai refuses an empty models list on a route with no installed
 * catalog) and every Lore selection names the provider explicitly, so the
 * stale entry is never chosen. */
export async function ensureModelEntry(
  settings: SettingsSeam, model: string, contextWindow: number,
  maxOutputTokens: number, images: boolean,
  effortLevels: readonly string[] | null,
  ifAbsent = false,
  scaffold: LoreRouteScaffold | null = null,
  compat: Record<string, unknown> | null = null,
  route: string = ROUTE,
): Promise<void> {
  const run = settingsWriteChain.then(async () => {
    // Re-read INSIDE the chain: a concurrent turn's committed entry must be
    // the base this upsert computes from. describe() merges the inherited
    // config layer (the home patch's llm-pi-ai row) with the profile patch
    // the settings service owns — the read sees every committed entry.
    const section = settings.describe().find((row) => row.ns === LLM_NS)?.value as {
      providers?: Record<string, { models?: CatalogModelEntry[] } & Record<string, unknown>>
    } | undefined
    const routeSection = section?.providers?.[route]
    const models: CatalogModelEntry[] = routeSection?.models ?? []
    const committed = models.find((m) => m.id === model)
    // The boot write exists to land ids the static layer does not declare —
    // checked inside the chain so a concurrent first turn is the writer, not
    // a race between both.
    if (ifAbsent && committed && typeof routeSection?.baseURL === 'string') return
    // A garbage cap (NaN from a malformed payload) must not poison the whole
    // section — omit it and let dsh fall back to its default. Sanitized ONCE
    // so the skip check compares against exactly what a write would land;
    // comparing the raw cap made every garbage-cap turn rewrite the entry.
    const cap = Number.isSafeInteger(contextWindow) && contextWindow > 0
      ? contextWindow
      : undefined
    const maxOut = Number.isSafeInteger(maxOutputTokens) && maxOutputTokens > 0
      ? maxOutputTokens
      : undefined
    // The effort declaration: null = gateway unreadable/unknown — carry the
    // committed one verbatim; a list (empty included) = the whole offer.
    const efforts = effortLevels === null
      ? committed?.reasoningEfforts
      : reasoningEffortsDeclaration(effortLevels, model)
    // The route scaffold rides this write whenever a field a turn can change
    // differs: baseURL (an admin edit) or apiKeyEnv (a route committed under
    // the pre-admin ref).
    const scaffoldPatch = scaffold !== null
      && (routeSection?.baseURL !== scaffold.baseURL || routeSection?.apiKeyEnv !== scaffold.apiKeyEnv)
      ? scaffold
      : {}
    // Skip when the committed entry already carries every value this turn
    // would write. The image modality is only REQUIRED on image turns: a
    // text-only turn leaves an image-capable entry untouched (no rewrite, no
    // strip) — the next image turn re-declares it before its own followup.
    if (!Object.keys(scaffoldPatch).length && committed && committed.contextWindow === cap
      && committed.maxTokens === maxOut
      && (effortLevels === null || _sameEfforts(committed.reasoningEfforts, efforts))
      && _sameCompat(committed.compat, compat ?? undefined)
      && (!images || (Array.isArray(committed.input)
        && committed.input.includes('image')))) return
    const entry: Record<string, unknown> = {
      id: model,
      ...(images ? { input: ['text', 'image'] } : {}),
      ...(efforts === undefined ? {} : { reasoningEfforts: efforts }),
      ...(compat === null ? {} : { compat }),
      ...(cap === undefined ? {} : { contextWindow: cap }),
      ...(maxOut === undefined ? {} : { maxTokens: maxOut }),
    }
    await settings.update(LLM_NS, {
      providers: { [route]: {
        ...scaffoldPatch,
        models: [...models.filter((m) => m.id !== model), entry],
      } },
    })
  })
  settingsWriteChain = run.then(() => undefined, () => undefined)
  await run
}

/** The credentials seam the key writes talk to (ctx.get('credentials');
 * duck-typed for tests). */
export interface CredentialsSeam {
  resolve(ref: string): Promise<{ value: string } | undefined>
  set(ref: string, value: string): Promise<void>
  unset(ref: string): Promise<void>
}

/** Store the turn's gateway key under AGENT_KEY_REF when it differs from the
 * stored one. llm-pi-ai resolves the route's apiKeyEnv through this service
 * per request, so the next request uses the new key with no restart. A
 * missing service fails loud: without it llm-pi-ai reads only the launch
 * environment, which never carries the ref. */
export async function ensureAgentKey(
  credentials: CredentialsSeam | undefined, key: string,
): Promise<void> {
  if (!credentials) {
    throw new Error('the credentials service is not composed — cannot store the agent API key')
  }
  if ((await credentials.resolve(AGENT_KEY_REF))?.value === key) return
  await credentials.set(AGENT_KEY_REF, key)
}

/** The config-editor seam the per-turn composition edits talk to
 * (ctx.configEditor; duck-typed for tests). */
export interface ConfigEditorSeam {
  entries(): Array<{ options: { id: string, config?: Record<string, unknown> } }>
  edit(
    entry: { options: { id: string, config?: Record<string, unknown> } },
    change: (
      current: Record<string, unknown>,
      inherited: Record<string, unknown>,
    ) => Record<string, unknown>,
  ): Promise<void>
}

/** One editable entry's live config, or undefined when the composition does
 * not list it (the editor only reaches entries it lists). */
function entryConfig(
  configEditor: ConfigEditorSeam, id: string,
): { options: { id: string, config?: Record<string, unknown> } } | undefined {
  return configEditor.entries().find((entry) => entry.options.id === id)
}

/**
 * Apply the turn's web-search provider and credential with dsh's own calls,
 * before the agent runs: a non-empty credential lands under the ONE ref when
 * it differs (ensureAgentKey's shape — the providers and dsh's DeepSeek row
 * resolve it per call), an EMPTY credential UNSETS the ref (the off switch:
 * credentials-local refuses to store ''; a missing ref reads as the loud
 * no-key failure), and the `web` entry's `searchProvider` is edited only when
 * it differs (the edit reloads the entry live and the provider plugins
 * re-register) — an admin change reaches the next call with no restart.
 * A missing seam fails loud: the pin and the credential are not optional.
 */
export async function ensureWebSearch(
  configEditor: ConfigEditorSeam | undefined,
  credentials: CredentialsSeam | undefined,
  provider: string, credential: string,
): Promise<void> {
  if (!configEditor) {
    throw new Error('the config editor is not composed — cannot pin the web-search provider')
  }
  if (!credentials) {
    throw new Error('the credentials service is not composed — cannot store the web-search key')
  }
  const stored = (await credentials.resolve(WEB_SEARCH_KEY_REF))?.value
  if (credential === '') {
    if (stored !== undefined) await credentials.unset(WEB_SEARCH_KEY_REF)
  } else if (stored !== credential) {
    await credentials.set(WEB_SEARCH_KEY_REF, credential)
  }
  const entry = entryConfig(configEditor, 'web')
  if (!entry) {
    throw new Error('the web entry is not configurable — cannot pin the web-search provider')
  }
  if ((entry.options.config ?? {}).searchProvider === provider) return
  await configEditor.edit(entry, (current) => ({ ...current, searchProvider: provider }))
}

/**
 * Apply the turn's title model to the `session-title-llm` entry: the pair
 * `{provider, model}` when the payload names one (the provider names the
 * TITLE model's own route — an anthropic-api title model rides
 * `providers.anthropic`), the pair removed when it is empty (the titler then
 * rides the session's own logged route). Edits only on difference; the
 * composition's profile row carries no pair, so the pair on disk is always
 * per-turn state. Runs AFTER ensureTitleEntry declared the model in its
 * route's catalog.
 */
export async function ensureTitleConfig(
  configEditor: ConfigEditorSeam | undefined, titleModel: string,
  provider: string = PROVIDER,
): Promise<void> {
  if (!configEditor) {
    throw new Error('the config editor is not composed — cannot apply the title model')
  }
  const entry = entryConfig(configEditor, 'session-title-llm')
  if (!entry) {
    throw new Error('the session-title-llm entry is not configurable — cannot apply the title model')
  }
  const current = entry.options.config ?? {}
  if (titleModel) {
    if (current.provider === provider && current.model === titleModel) return
    await configEditor.edit(entry, (cur) => ({ ...cur, provider, model: titleModel }))
    return
  }
  if (current.provider === undefined && current.model === undefined) return
  await configEditor.edit(entry, (cur) => {
    const next = { ...cur }
    delete next.provider
    delete next.model
    return next
  })
}

/** Declare the turn's title model (payload `title_model` — admin CHAT_TITLE_MODEL,
 * else the backend's CHAT_MODEL) in the title model's OWN route catalog
 * (provider + scaffold as resolved by the turn — an anthropic-api title
 * model lands under `providers.anthropic`): pi-ai refuses an id outside the
 * catalog, and a fresh install's first route write is a turn, not the boot. A
 * reasoning model is accepted: its thinking rides the titler's
 * maxOutputTokens budget in the profile patch.
 * WHY ifAbsent also for compat: a committed title entry is never rewritten,
 * so a compat the router adds later reaches a title-only model on its first
 * chat turn, not here — accepted, a titler call carries no effort and the
 * stale entry only affects how its thinking is shaped. */
export async function ensureTitleEntry(
  settings: SettingsSeam, model: string, provider: string,
  scaffold: LoreRouteScaffold | null,
  compat: Record<string, unknown> | null = null,
): Promise<void> {
  if (!model) return
  await ensureModelEntry(
    settings, model, Number.NaN, Number.NaN, false, null, true, scaffold,
    compat, provider)
}

// ── The catalog upsert.
//
// # ARCH: pi-ai refuses a model id absent from the route's catalog
//   (UNKNOWN_MODEL), and the composition declares NO static route at all
//   (llm-pi-ai mounts dormant) — every TURN is the route's writer
//   (prepareUserContent): the scaffold (loreRoute/anthropicRoute over the
//   turn's gateway, picked per model by ROUTE_BY_API) and the model's
//   id-only entry land there, ifAbsent semantics nowhere —
//   a committed entry (a previous turn already filled its caps/efforts) is
//   left untouched, never downgraded to id-only; the caps numbers, compat
//   and effort levels land on each model's first turn (ensureModelEntry).
//   The title model (payload title_model, applied per turn by
//   ensureTitleConfig) is declared by each turn too (ensureTitleEntry) —
//   under its OWN route, which may differ from the turn model's. There is
//   no boot writer and no env model to write: the model comes only from the
//   turn — the backend resolves body.model →
//   session row → admin CHAT_MODEL and refuses a model-less turn itself.

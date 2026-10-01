/**
 * The driver's model-capability resolution — the ONE source.
 *
 * # ARCH: the plugin owns the model-facing semantics. The gateway's /v1/models
 * metadata (`context_length`, `max_completion_tokens`, image input modalities)
 * plus the /v1/capabilities reasoning map (`effort_levels` per model) are
 * resolved HERE — TTL-cached, single-flight — and served two ways: the
 * /capability endpoint answers the backend's vision gate, and the turn handler
 * arms its own catalog upsert (ensureModelEntry) plus the context_usage
 * denominator. Python resolves nothing: no backend resolver re-reads this
 * same gateway payload — the gates this module feeds are its only readers.
 *
 * Failure semantics: a gateway the plugin cannot read answers UNKNOWN
 * (vision false, no numbers, no levels) with a loud log — strip + warn,
 * never a raised exception — and the turn then runs on the composition's
 * NAMED defaults
 * (defaultContextWindow in cordis.patch.yml, dsh's own default maxTokens)
 * rather than a second guess in code. An id the gateway does not serve is the
 * same UNKNOWN, logged. A /capabilities failure alone keeps the /v1/models
 * numbers and nulls only the levels (feature absence, mirroring the backend
 * picker's own {} on a capabilities miss).
 */

/** The capability answer for one model id. */
export interface ModelCaps {
  /** The gateway's /v1/models lists this id. */
  known: boolean
  /** The model takes image parts (the backend's vision gate reads this). */
  vision: boolean
  /** The gateway's `context_length`, or null when the entry omits/abuses it. */
  contextWindow: number | null
  /** The gateway's `max_completion_tokens`, or null when omitted. When null the
   * plugin writes NO maxTokens into the catalog entry and dsh's own default
   * applies — never a floor guess. */
  maxOutputTokens: number | null
  /** The gateway /capabilities `effort_levels` list in GATEWAY spellings, or
   * null when the model is absent from the map (unknown model, non-reasoning
   * by the gateway's own word, or an unreadable endpoint). [] and null both
   * mean "no level is offered"; null additionally means "do not touch the
   * committed declaration" for the catalog upsert. */
  effortLevels: readonly string[] | null
}

const UNKNOWN: ModelCaps = {
  known: false, vision: false, contextWindow: null, maxOutputTokens: null,
  effortLevels: null,
}

/** The AI gateway one request runs against. */
export interface Gateway {
  base: string
  key: string
}

/** The gateway from the backend's per-request values (admin panel, else the
 * backend's env); the harness's own env is only the fallback for a value the
 * request does not carry. */
export function gatewayOf(url: unknown, key: unknown): Gateway {
  const given = (v: unknown): string => (typeof v === 'string' ? v.trim() : '')
  return {
    base: (given(url) || (process.env.LORE_AI_API_URL || '').trim()).replace(/\/+$/, ''),
    key: given(key) || process.env.AI_API_KEY || '',
  }
}

/** Injectable seams (tests fake the fetch and the clock; prod uses globals).
 * `gateway` absent = the harness env's (gatewayOf with nothing given). */
export interface CapsIo {
  fetch?: typeof fetch
  now?: () => number
  gateway?: Gateway
}

const CACHE_TTL_MS = 60_000

// WHY keyed by the gateway: an admin change of URL or key must not be answered
// by the previous gateway's catalog for the rest of the TTL.
let cache: { at: number; gw: string; entries: unknown[] } | null = null
let inFlight: { gw: string; p: Promise<unknown[]> } | null = null

// The /capabilities twin of the /models cache: same TTL, same single-flight,
// same gateway key, its own module state. Value = the effort_levels list in
// GATEWAY spellings; an entry with supported=false stores [] (definitely
// non-reasoning), a model absent from the map reads null (unknown) via
// resolveModelCaps.
let effortCache: { at: number; gw: string; map: Record<string, string[]> } | null = null
let effortInFlight: { gw: string; p: Promise<Record<string, string[]>> } | null = null

function _gwKey(gw: Gateway): string {
  return `${gw.base}\n${gw.key}`
}

function _positiveInt(value: unknown): number | null {
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isSafeInteger(n) && n > 0 ? n : null
}

function _entryVision(entry: Record<string, unknown>): boolean {
  // INVARIANT (ported from the deleted Python twin): an entry declaring
  // NEITHER `supports_vision` nor image `input_modalities` is non-vision — a
  // model that rejects image parts fails the whole turn with an opaque 400, so
  // the safe failure direction is strip + warn, never send.
  const flag = entry.supports_vision
  if (typeof flag === 'boolean') return flag
  const modalities = (entry.architecture as Record<string, unknown> | undefined)
    ?.input_modalities
  return Array.isArray(modalities) && modalities.includes('image')
}

async function _gatewayEntries(io: CapsIo): Promise<unknown[]> {
  const doFetch = io.fetch ?? fetch
  const now = io.now ?? Date.now
  const gw = io.gateway ?? gatewayOf(undefined, undefined)
  const gwKey = _gwKey(gw)
  if (cache && cache.gw === gwKey && now() - cache.at < CACHE_TTL_MS) return cache.entries
  if (inFlight && inFlight.gw === gwKey) return inFlight.p
  const p = (async () => {
    const resp = await doFetch(`${gw.base}/models`, {
      headers: { Authorization: `Bearer ${gw.key}` },
    })
    if (!resp.ok) throw new Error(`gateway /models HTTP ${resp.status}`)
    const body = await resp.json() as { data?: unknown }
    const entries = Array.isArray(body?.data) ? body.data : []
    cache = { at: now(), gw: gwKey, entries }
    return entries
  })()
  inFlight = { gw: gwKey, p }
  try {
    return await p
  } finally {
    if (inFlight?.p === p) inFlight = null
  }
}

async function _gatewayEffortMap(io: CapsIo): Promise<Record<string, string[]>> {
  const doFetch = io.fetch ?? fetch
  const now = io.now ?? Date.now
  const gw = io.gateway ?? gatewayOf(undefined, undefined)
  const gwKey = _gwKey(gw)
  if (effortCache && effortCache.gw === gwKey && now() - effortCache.at < CACHE_TTL_MS) {
    return effortCache.map
  }
  if (effortInFlight && effortInFlight.gw === gwKey) return effortInFlight.p
  const p = (async () => {
    const resp = await doFetch(`${gw.base}/capabilities`, {
      headers: { Authorization: `Bearer ${gw.key}` },
    })
    if (!resp.ok) throw new Error(`gateway /capabilities HTTP ${resp.status}`)
    const body = await resp.json() as unknown
    const out: Record<string, string[]> = {}
    // Same softness as the backend picker twin (_normalize_reasoning_map): a
    // non-dict body or a garbage entry is feature absence, never a failed turn.
    if (body && typeof body === 'object' && !Array.isArray(body)) {
      for (const [mid, entry] of Object.entries(body as Record<string, unknown>)) {
        if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue
        const e = entry as { supported?: unknown; effort_levels?: unknown }
        if (e.supported !== true) {
          out[mid] = []
          continue
        }
        out[mid] = Array.isArray(e.effort_levels)
          ? e.effort_levels.filter((lv): lv is string => typeof lv === 'string')
          : []
      }
    }
    effortCache = { at: now(), gw: gwKey, map: out }
    return out
  })()
  effortInFlight = { gw: gwKey, p }
  try {
    return await p
  } finally {
    if (effortInFlight?.p === p) effortInFlight = null
  }
}

/** Resolve one model's capability off the gateway /v1/models roster and the
 * /v1/capabilities reasoning map. */
export async function resolveModelCaps(
  model: string,
  io: CapsIo = {},
): Promise<ModelCaps> {
  if (!model) return { ...UNKNOWN }
  let entries: unknown[]
  try {
    entries = await _gatewayEntries(io)
  } catch (err) {
    // Loud degrade, never a thrown gate: the backend strips + warns, the turn
    // runs on the composition's named defaults.
    console.error(`[lore-caps] gateway /models unreadable: ${String(err)}`)
    return { ...UNKNOWN }
  }
  const entry = entries.find(
    (e) => (e as Record<string, unknown> | null)?.id === model,
  ) as Record<string, unknown> | undefined
  if (!entry) {
    console.error(
      `[lore-caps] model "${model}" not served by the gateway — running on the composition defaults (defaultContextWindow, dsh maxTokens)`)
    return { ...UNKNOWN }
  }
  // The levels ride the same resolution but never fail it: a /capabilities
  // miss is feature absence (null levels), the roster numbers above stand.
  let effortLevels: readonly string[] | null = null
  try {
    const map = await _gatewayEffortMap(io)
    effortLevels = map[model] ?? null
  } catch (err) {
    console.error(`[lore-caps] gateway /capabilities unreadable: ${String(err)}`)
  }
  return {
    known: true,
    vision: _entryVision(entry),
    contextWindow: _positiveInt(entry.context_length),
    maxOutputTokens: _positiveInt(entry.max_completion_tokens),
    effortLevels,
  }
}

// ── The pi-ai effort-level vocabulary ─────────────────────────────────────────
//
// # INVARIANT: this vocabulary is the TWIN of the backend picker filter
//   (PI_AI_EFFORT_LEVELS + _EFFORT_SYNONYMS in backend/routes/chat/
//   models_catalog.py). Why: the picker must never offer a level the adapter
//   would refuse — pi-ai fails an undeclared level with
//   UNSUPPORTED_REASONING_EFFORT before network I/O — and the adapter must
//   never declare a level the picker did not offer. The two sets move in ONE
//   commit or turns start dying at the seam. An offer is nameable iff it
//   names a level beyond `off`: pi-ai refuses an off-only declaration and a
//   failed catalog resolution takes the WHOLE provider route out, so an
//   off-only offer declares nothing — the model runs non-reasoning and its
//   turns die one-model-loud, never route-wide.

/** pi-ai's canonical level names — the KEY space of a reasoningEfforts
 * declaration. The VALUE (wire spelling) is the gateway's own. */
export const PI_AI_EFFORT_LEVELS: readonly string[] = [
  'off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max',
]

/** Gateway level spellings outside pi-ai's set that still name a level
 * (key = gateway spelling, value = the pi-ai level it means). */
const EFFORT_SYNONYMS: Readonly<Record<string, string>> = { none: 'off' }

/** The pi-ai level KEY for a gateway effort spelling, or null when pi-ai
 * cannot name it (never offered, never declared). */
export function piAiEffortKey(gatewayLevel: string): string | null {
  if (PI_AI_EFFORT_LEVELS.includes(gatewayLevel)) return gatewayLevel
  return EFFORT_SYNONYMS[gatewayLevel] ?? null
}

/** The reasoningEfforts declaration for one model's advertised levels: pi-ai
 * key → the gateway's wire spelling (identity for pi-ai's own names, e.g.
 * `none` → `off: none` — Off offered, the wire carries the gateway's word).
 * A level pi-ai cannot name is dropped with one log line per model — never
 * offered; the twin backend filter keeps it out of the picker too. undefined
 * for an empty offer (an entry without the field does not reason) and for an
 * offer naming no level beyond `off`: pi-ai refuses an off-only declaration
 * and a failed catalog resolution takes the whole provider route out, so the
 * model runs non-reasoning instead. */
export function reasoningEffortsDeclaration(
  levels: readonly string[], model: string,
): Record<string, string> | undefined {
  if (levels.length === 0) return undefined
  const decl: Record<string, string> = {}
  const dropped: string[] = []
  for (const level of levels) {
    const key = piAiEffortKey(level)
    if (key === null) {
      dropped.push(level)
      continue
    }
    decl[key] = level
  }
  if (!Object.keys(decl).some((k) => k !== 'off')) {
    console.error(`[lore-caps] effort offer of ${model} names no pi-ai level `
      + `beyond off${dropped.length > 0 ? ` (${dropped.map((l) => `"${l}"`).join(', ')} unnameable)` : ''} `
      + '— pi-ai refuses an off-only declaration, the model runs non-reasoning')
    return undefined
  }
  if (dropped.length > 0) {
    console.error(`[lore-caps] effort level${dropped.length > 1 ? 's' : ''} `
      + `${dropped.map((l) => `"${l}"`).join(', ')} of ${model} has no pi-ai key — not offered`)
  }
  return decl
}

/** Gate for the title model: the titler names no effort, and on this
 * openai-format route a request with no `reasoning_effort` gets the
 * PROVIDER's default — there is no per-purpose way to say "don't think", so a
 * model advertising levels would think on every title call. The caller
 * refuses that title-model write loudly (the harness never exits on
 * configuration); an unreadable gateway (effortLevels null) passes — the
 * caller warns instead of gating blind. */
export function assertNonReasoningTitleModel(caps: ModelCaps, model: string): void {
  if (caps.effortLevels && caps.effortLevels.length > 0) {
    throw new Error(
      `CHAT_TITLE_MODEL "${model}" advertises reasoning effort levels `
      + `[${caps.effortLevels.join(', ')}] — the title call names no effort, so the provider's `
      + 'default would think on every title. Set the session title model in Admin panel → '
      + 'Models & APIs to a non-reasoning model (supported=false on the gateway).')
  }
}

/** Test seam: drop the TTL caches + any in-flight fetches. */
export function resetCapsCache(): void {
  cache = null
  inFlight = null
  effortCache = null
  effortInFlight = null
}

/**
 * Lore driver plugin — the RENTED agent loop behind Lore's driver contract.
 *
 * # SYSTEM: harness-driver — the rented brain's driver: serves the HTTP
 *   endpoints the Lore backend speaks (`POST /followup` + `POST /stop` — the
 *   driver-owned turn, `POST /session-leaf`, `POST /session-entries`,
 *   `GET/POST /approvals`, `POST /approvals/resolve`, `GET /capability`, `GET
 *   /health`) plus the standing event channel `/ws/events`
 *   (subscribe/unsubscribe by session id — ws-events.ts).
 *   Lore owns identity/RBAC/persistence
 *   and its tools — they arrive as payload
 *   proxies (tools.ts) with the skills pack split (skills.ts), per-turn system
 *   prompt handed over by Lore's own prep layer (registered as the agent's
 *   COMPLETE prompt section — no second copy grows here). The turn pipeline
 *   drives the dsh Agent per turn (resume-or-create → followup → quiescence →
 *   flush → dispose): stateless on our side between turns, the canonical
 *   conversation lives in the dsh session log (JSONL under $DSH_HOME/sessions
 *   — a HOST bind mount in compose).
 *
 * # ARCH: Lore session id → dsh session id. The FIRST turn uses the Lore id
 *   itself as the dsh session id; every leaf move (chat fork / root fork) seeds
 *   a NEW dsh session under `$DSH_HOME/lore-session-map.json` and repoints the
 *   mapping. Entry ids served to Lore (`e<seq>`) are stable across forks: a
 *   fork's seed RETAINS the parent prefix with the same seqs.
 */

import { randomUUID } from 'node:crypto'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import http from 'node:http'
import type { Context } from '@deepseek-ai/cordis'
import { buildForkSeed, SessionId, SessionSeq } from '@deepseek-ai/dsh-session'
import { createUserMessage, ReasoningEffortId } from '@deepseek-ai/dsh-llm'
import type { ContentBlock } from '@deepseek-ai/dsh-llm'
import { installModelSelection } from '@deepseek-ai/dsh-agent'
import type { ModelSelectionRef } from '@deepseek-ai/dsh-agent'
import type { PreStepDecision } from '@deepseek-ai/dsh-agent'
import type { ImageAttachmentRef, ImageMediaType } from '@deepseek-ai/dsh-attachment'

import { mapEvent, newTurnMapState, turnFailureLine } from './map.ts'
import { loreEvent } from '../../lore-conversation/src/lore-events.ts'
import { lastTurnEndSeq } from './leaf.ts'
import {
  gatewayOf, piAiEffortKey,
  reasoningEffortsDeclaration, resolveModelCaps,
  type Gateway,
} from './caps.ts'
import { projectSessionEntries } from './entries.ts'
import { parseTimeStamps, timeStampMessage } from './time-stamps.ts'
import { createSessionStreamBaselines, type SessionStreamBaselines } from './stream-baselines.ts'
import {
  attachEventsChannel, createSessionEventTap, loadWs, relayAssistantStream,
  type EventsChannel, type SessionEventTap,
} from './ws-events.ts'
import {
  pendingHoldsFor, registerLoreApprovalBridge, resolveSessionAsks, resolveVerdict,
} from './approvals.ts'
// INVARIANT: never import boot-env.ts here. Why: it is the process entry and
// awaits runCli() at top level, which loads this plugin — importing it back
// waits on its own unfinished evaluation and the harness never listens.
import { WEB_SEARCH_KEY_REF } from './web-search/key.ts'
import {
  assembleTurnPrompt, catalogServedNames, computeCoreTools, computeActiveTools, loreSkillProvider,
  resolveSkillCatalog, restrictDenyNames, restoreActivatedSkills, skillLoadCallName,
  skillLoadResultName, skillsDelta, type ResolvedSkill, type SkillCatalogWire,
} from './skills.ts'
import {
  callToolApi, clearSessionToolCtx, clearTurnRequestCtx,
  disposeLoreTools, loreTurnCtxFor, registerLoreTools,
  registeredLoreToolNames, setSessionToolCtx,
  stampTurnRequestCtx,
} from './tools.ts'
export const name = 'lore-driver'
export const inject = ['agents', 'approval', 'sessionPersistence', 'sessions', 'skills', 'systemPrompt', 'tools', 'subagents']

const PORT = Number(process.env.PORT || 8090)
const SECRET = process.env.LORE_DRIVER_SECRET || ''
/** The LLM route this line drives: the `lore` provider route of the llm-pi-ai
 * adapter, as configured in cordis.patch.yml. Every model id the gateway
 * serves is upserted into the route's catalog by the plugin itself
 * (ensureModelEntry) — pi-ai refuses an id outside the catalog
 * (UNKNOWN_MODEL), which is what makes the upsert the seam it runs on. */
export const PROVIDER = 'lore'

/** lore session id → current dsh session id (persisted beside the session
 * logs, under the mounted sessions root — survives container replacement). */
class SessionMap {
  private file: string
  private map = new Map<string, string>()

  constructor(home: string | undefined) {
    this.file = join(home ?? process.cwd(), 'sessions', 'lore-session-map.json')
    try {
      if (existsSync(this.file)) {
        const raw = JSON.parse(readFileSync(this.file, 'utf8')) as Record<string, string>
        for (const [k, v] of Object.entries(raw)) this.map.set(k, v)
      }
    } catch {
      // Unreadable map = treat as empty; the first turn re-registers the id.
    }
  }

  get(loreId: string): string {
    return this.map.get(loreId) ?? loreId
  }

  set(loreId: string, dshId: string): void {
    this.map.set(loreId, dshId)
    try {
      writeFileSync(this.file, JSON.stringify(Object.fromEntries(this.map), null, 2))
    } catch (err) {
      console.error(`[lore-driver] session map write failed: ${String(err)}`)
    }
  }

  /** Drop one lore session's mapping (the session-delete forward). The map is
   * the identity bridge lore session → dsh session; a deleted session leaves
   * its entry behind forever otherwise (one entry per ever-created session,
   * unbounded on a long-lived container). */
  delete(loreId: string): void {
    if (!this.map.delete(loreId)) return
    try {
      writeFileSync(this.file, JSON.stringify(Object.fromEntries(this.map), null, 2))
    } catch (err) {
      console.error(`[lore-driver] session map write failed: ${String(err)}`)
    }
  }
}

function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let raw = ''
    req.setEncoding('utf8')
    req.on('data', (c: string) => { raw += c })
    req.on('end', () => resolve(raw))
    req.on('error', reject)
  })
}

/** A turn the harness cannot run as configured (no model, no gateway URL or
 * key). The followup answers it as an accepted turn whose only frames are the
 * error naming the admin setting and the close — the backend has already bound
 * the turn, so the person reads the reason instead of "service unreachable". */
export class TurnConfigError extends Error {
  constructor(readonly dshId: string, message: string) {
    super(message)
  }
}

const ADMIN_MODELS = 'Admin panel → Models & APIs'

/** The turn's configuration refusal, or null when it can run: every value
 * arrives from the backend per turn (admin panel, else the backend's env) —
 * the harness holds no fallback of its own (plan
 * component-wiring-not-settings step 3). */
export function turnConfigProblem(model: string, gateway: Gateway): string | null {
  if (!model) return `No chat model is set — set it in ${ADMIN_MODELS}.`
  if (!gateway.base) return `No AI API URL is set — set it in ${ADMIN_MODELS}.`
  if (!gateway.key) return `No AI API key is set — set it in ${ADMIN_MODELS}.`
  return null
}

// ── The multimodal turn prompt.
//
// # ARCH: an image_url part ARRIVING is the backend's assertion that this
// model takes images — the backend's vision gate (capability from the
// driver's /capability reply, caps.ts) already stripped the images otherwise
// and warned `image_not_delivered`. The plugin carries NO capability field and NO
// media-type list: it persists each data-URI image through the durable
// attachment seam (dsh sniffs the bytes and rejects a declared type that does
// not match them), and a part that cannot become an image block becomes a
// text block naming what was lost — the whole bug this replaces was a silent
// drop.

/** One image admission into the durable attachment store (ctx.attachments). */
export type SaveImage = (input: {
  data: Uint8Array
  mediaType: string
}) => Promise<ImageAttachmentRef>

const IMAGE_DATA_URI_RE = /^data:(image\/[a-z0-9.+-]+);base64,(.*)$/is

function notDelivered(reason: string): ContentBlock {
  return { type: 'text', text: `[attachment not delivered: ${reason}]` }
}

/** dsh content blocks out of the turn contract's `prompt` (string | block
 * list: text parts + image_url data-URI parts). `images` counts the image
 * blocks actually persisted — the signal for the per-turn capability upsert. */
export async function promptContent(
  prompt: unknown,
  saveImage: SaveImage,
): Promise<{ blocks: ContentBlock[]; images: number }> {
  if (typeof prompt === 'string') return { blocks: [{ type: 'text', text: prompt }], images: 0 }
  if (!Array.isArray(prompt)) return { blocks: [{ type: 'text', text: '' }], images: 0 }
  const blocks: ContentBlock[] = []
  let images = 0
  for (const part of prompt as unknown[]) {
    if (typeof part === 'string') {
      if (part !== '') blocks.push({ type: 'text', text: part })
      continue
    }
    if (part === null || typeof part !== 'object') {
      blocks.push(notDelivered('unusable prompt part'))
      continue
    }
    const p = part as Record<string, unknown>
    if (p.type === 'text') {
      const text = typeof p.text === 'string' ? p.text : ''
      if (text !== '') blocks.push({ type: 'text', text })
      continue
    }
    if (p.type === 'image_url') {
      const url = (p.image_url as Record<string, unknown> | undefined)?.url
      const match = typeof url === 'string' ? IMAGE_DATA_URI_RE.exec(url) : null
      if (!match) {
        blocks.push(notDelivered('image url is not a data URI'))
        continue
      }
      // Node's base64 decoder never throws — it skips what it cannot read, so
      // a corrupt payload decodes to garbage BYTES and the store's sniff is
      // what names the loss (INVALID_IMAGE). No catch here to dead-code.
      const data = Buffer.from(match[2], 'base64')
      try {
        const ref = await saveImage({ data, mediaType: match[1].toLowerCase() })
        blocks.push({ type: 'image', attachment: ref })
        images += 1
      } catch (err) {
        // The store's stable code (INVALID_IMAGE, IMAGE_TYPE_MISMATCH, …) is
        // the reason — the model, and the session log, see exactly what was
        // lost and why.
        const code = (err as { code?: unknown } | null)?.code
        blocks.push(notDelivered(
          typeof code === 'string' ? code : `save failed: ${String(err)}`))
      }
      continue
    }
    blocks.push(notDelivered(
      `unsupported prompt part${typeof p.type === 'string' ? ` "${p.type}"` : ''}`))
  }
  if (blocks.length === 0) blocks.push({ type: 'text', text: '' })
  return { blocks, images }
}

// ── The per-turn image-capability upsert.
//
// # ARCH: pi-ai's request modalities default to [text] — a model entry
// without an `input` field accepts text only and the harness refuses an
// image before it is attached — so a turn carrying image blocks must declare
// its model image-capable in the `llm-pi-ai.providers.lore` settings section
// (the entry's `input` field) — the ONLY seam; the adapter re-reads its
// profiles per request, so the entry reaches the very next call with no
// restart. The section ACCUMULATES entries keyed by model id (no cap, no
// eviction): a single-entry catalog would let a turn on model Y evict X's
// entry between X's write and X's dispatch and fail X with a refused image
// attachment.

const LLM_NS = 'llm-pi-ai'
/** The one provider route this composition declares (cordis.patch.yml,
 * llm-pi-ai config): the settings entry path is `providers.<ROUTE>.models`. */
const ROUTE = 'lore'

/** # INVARIANT: the `models` array is replaced WHOLESALE by a settings
 * update, so every read-compute-write upsert must run exclusively behind this
 * chain. Why: the settings service serializes writes per namespace but cannot
 * merge two stale reads — without the chain, two concurrent image turns on
 * different models each compute from the same snapshot and one entry is
 * silently lost. A lost entry is self-healing (the next turn on that model
 * upserts again), so this bounds a retry, not a corruption. */
let settingsWriteChain: Promise<unknown> = Promise.resolve()

/** One catalog entry as the `llm-pi-ai.providers.lore` settings section shapes
 * it: pi-ai's field names (`input`, `reasoningEfforts`), not the old route's. */
interface CatalogModelEntry {
  id: string
  input?: readonly string[]
  contextWindow?: number
  maxTokens?: number
  reasoningEfforts?: Record<string, string>
}

/** The `lore` provider scaffold declared beside the model entries: everything
 * the route needs that is NOT a model. The static fields are constants;
 * baseURL comes from the turn (gatewayOf), because the composition cannot
 * carry it (see cordis.patch.yml's llm-pi-ai note: `providers` is volatile,
 * and a `!!js` expression inside the volatile subtree breaks every settings
 * write). */
export interface LoreRouteScaffold {
  apiKeyEnv: string
  api: string
  baseURL: string
  compat: Record<string, unknown>
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
 * is not a knob anyone sets (plan component-wiring-not-settings step 3).
 * Same number the composes' deleted env default carried: pi-ai's own
 * fallback (262144) would silently change the context_usage denominator. */
export const ROUTE_DEFAULT_CONTEXT_WINDOW = 128000

/** The route scaffold for one gateway base; null when there is none (no
 * fallback — the turn is refused, never pretends to a route). */
export function loreRoute(baseURL: string): LoreRouteScaffold | null {
  if (!baseURL) return null
  return {
    apiKeyEnv: AGENT_KEY_REF,
    api: 'openai-completions',
    baseURL,
    compat: {
      supportsDeveloperRole: false,
      maxTokensField: 'max_tokens',
      thinkingFormat: 'openai',
    },
    defaultContextWindow: ROUTE_DEFAULT_CONTEXT_WINDOW,
  }
}

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

/** Upsert the model's catalog entry into the `llm-pi-ai.providers.lore`
 * settings section — `{id, contextWindow, maxTokens}` plus
 * `input: ['text','image']` ONLY when the turn carried images, and
 * `reasoningEfforts` (pi-ai key → gateway wire spelling) whenever the gateway
 * advertises levels for the model (null levels = no information: the
 * committed declaration rides along untouched); skips the write when the
 * committed entry already carries exactly those values. Runs on EVERY turn:
 * the per-model `maxTokens` (the router's `max_completion_tokens`, resolved
 * by the driver itself — caps.ts) is the output cap dsh honors ahead of the
 * connection profile — without the entry every model ran at one hardcoded
 * number. `ifAbsent` (the title-entry path) writes ONLY when no entry
 * exists: a committed entry stays untouched instead of being downgraded to
 * id-only until the model's next turn.
 * `route` (loreRoute) rides the SAME write whenever the committed provider
 * entry's baseURL or apiKeyEnv differs from it: the route scaffold is
 * declared beside the models because pi-ai refuses a provider-less/empty
 * route — the composition mounts llm-pi-ai dormant and THIS is the only
 * writer — and the adapter re-reads its profiles per request, so an admin
 * change of the gateway reaches the next request with no restart. */
export async function ensureModelEntry(
  settings: SettingsSeam, model: string, contextWindow: number,
  maxOutputTokens: number, images: boolean,
  effortLevels: readonly string[] | null,
  ifAbsent = false,
  route: LoreRouteScaffold | null = null,
): Promise<void> {
  const run = settingsWriteChain.then(async () => {
    // Re-read INSIDE the chain: a concurrent turn's committed entry must be
    // the base this upsert computes from. describe() merges the inherited
    // config layer (the home patch's llm-pi-ai row) with the profile patch
    // the settings service owns — the read sees every committed entry.
    const section = settings.describe().find((row) => row.ns === LLM_NS)?.value as {
      providers?: Record<string, { models?: CatalogModelEntry[] } & Record<string, unknown>>
    } | undefined
    const provider = section?.providers?.[ROUTE]
    const models: CatalogModelEntry[] = provider?.models ?? []
    const committed = models.find((m) => m.id === model)
    // The boot write exists to land ids the static layer does not declare —
    // checked inside the chain so a concurrent first turn is the writer, not
    // a race between both.
    if (ifAbsent && committed && typeof provider?.baseURL === 'string') return
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
    const scaffold = route !== null
      && (provider?.baseURL !== route.baseURL || provider?.apiKeyEnv !== route.apiKeyEnv)
      ? route
      : {}
    // Skip when the committed entry already carries every value this turn
    // would write. The image modality is only REQUIRED on image turns: a
    // text-only turn leaves an image-capable entry untouched (no rewrite, no
    // strip) — the next image turn re-declares it before its own followup.
    if (!Object.keys(scaffold).length && committed && committed.contextWindow === cap
      && committed.maxTokens === maxOut
      && (effortLevels === null || _sameEfforts(committed.reasoningEfforts, efforts))
      && (!images || (Array.isArray(committed.input)
        && committed.input.includes('image')))) return
    const entry: Record<string, unknown> = {
      id: model,
      ...(images ? { input: ['text', 'image'] } : {}),
      ...(efforts === undefined ? {} : { reasoningEfforts: efforts }),
      ...(cap === undefined ? {} : { contextWindow: cap }),
      ...(maxOut === undefined ? {} : { maxTokens: maxOut }),
    }
    await settings.update(LLM_NS, {
      providers: { [ROUTE]: {
        ...scaffold,
        models: [...models.filter((m) => m.id !== model), entry],
      } },
    })
  })
  settingsWriteChain = run.then(() => undefined, () => undefined)
  await run
}

/** The user-message content for one turn: the mapped prompt blocks plus the
 * catalog upsert that must land BEFORE the followup. EVERY turn upserts (the
 * entry's `maxTokens` is the router-sourced output cap — see
 * ensureModelEntry); on image turns the same entry also declares image
 * capability (pi-ai's `[text]` input default would otherwise make the harness
 * refuse the attachment before it is attached); whenever the gateway
 * advertises effort levels the entry also carries the `reasoningEfforts`
 * declaration (an undeclared level dies in dsh before network I/O). A
 * missing settings seam fails LOUD on every turn — the cap is not optional
 * metadata. */
export async function prepareUserContent(
  prompt: unknown,
  saveImage: SaveImage,
  settings: SettingsSeam | undefined,
  model: string,
  contextWindow: number,
  maxOutputTokens: number,
  effortLevels: readonly string[] | null,
  route: LoreRouteScaffold | null = null,
): Promise<ContentBlock[]> {
  const { blocks, images } = await promptContent(prompt, saveImage)
  if (!settings) {
    throw new Error('the settings service is not composed — cannot declare the model catalog entry')
  }
  await ensureModelEntry(
    settings, model, contextWindow, maxOutputTokens, images > 0, effortLevels,
    false, route,
  )
  return blocks
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
 * `{provider: 'lore', model}` when the payload names one, the pair removed
 * when it is empty (the titler then rides the session's own logged route).
 * Edits only on difference; the composition's profile row carries no pair,
 * so the pair on disk is always per-turn state. Runs AFTER ensureTitleEntry
 * declared the model in the route's catalog.
 */
export async function ensureTitleConfig(
  configEditor: ConfigEditorSeam | undefined, titleModel: string,
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
    if (current.provider === 'lore' && current.model === titleModel) return
    await configEditor.edit(entry, (cur) => ({ ...cur, provider: 'lore', model: titleModel }))
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
 * else the backend's CHAT_MODEL) in the route's catalog: pi-ai refuses an id
 * outside it, and a fresh install's first route write is a turn, not the
 * boot. A reasoning model is accepted: its thinking rides the titler's
 * maxOutputTokens budget in the profile patch. */
export async function ensureTitleEntry(
  settings: SettingsSeam, model: string, route: LoreRouteScaffold | null,
): Promise<void> {
  if (!model) return
  await ensureModelEntry(settings, model, Number.NaN, Number.NaN, false, null, true, route)
}

// ── The catalog upsert.
//
// # ARCH: pi-ai refuses a model id absent from the route's catalog
//   (UNKNOWN_MODEL), and the composition declares NO static route at all
//   (llm-pi-ai mounts dormant) — every TURN is the route's writer
//   (prepareUserContent): the scaffold (loreRoute over the turn's gateway)
//   and the model's id-only entry land there, ifAbsent semantics nowhere —
//   a committed entry (a previous turn already filled its caps/efforts) is
//   left untouched, never downgraded to id-only; the caps numbers and effort
//   levels land on each model's first turn (ensureModelEntry). The title
//   model (payload title_model, applied per turn by ensureTitleConfig) is
//   declared by each turn too (ensureTitleEntry). There is no boot writer and
//   no env model to write: the model comes only from the turn (plan
//   component-wiring-not-settings step 3) — the backend resolves body.model →
//   session row → admin CHAT_MODEL and refuses a model-less turn itself.

export function apply(ctx: Context): void {
  const map = new SessionMap(process.env.DSH_HOME)
  // The ONE session-event tap: apply() holds the single
  // ctx.on('session/event') subscription; the standing WS channel and the
  // per-turn watch subscribe through the tap, so every relay sink sees the
  // same events in the same synchronous order.
  const tap = createSessionEventTap()
  const offTap = ctx.on('session/event', (session: any, ev: any) => tap.emit(session, ev))
  const server = http.createServer((req, res) => { void handleRequest(ctx, map, tap, channel, baselines, req, res) })
  // The reload-baseline fold's durable cursor: every session event observed
  // here advances the session's last-seen seq (watch phase — before the
  // channel's mapped frames), so a stream start records the seq it started
  // after (stream-baselines.ts observe).
  const baselines = createSessionStreamBaselines()
  const offObserve = tap.subscribe((session: any, ev: any) => {
    baselines.observe(
      String(session?.id ?? ''),
      typeof ev?.seq === 'number' ? ev.seq : undefined,
    )
  }, 'watch')
  // The approval bridge: dsh's approval/request waterfall is answered
  // by the parked question the verdict endpoints below resolve.
  registerLoreApprovalBridge(ctx, loreTurnCtxFor)
  // The standing event channel rides the SAME server and the SAME secret
  // gate; ws resolves lazily (see ws-events.ts's loadWs ARCH) — a missing ws
  // logs loudly, and the turn is refused (no delivery path) rather than run
  // silently into nothing.
  const ws = loadWs(process.env.DSH_HOME)
  const channel = ws
    ? attachEventsChannel({
      server, tap, resolve: (id) => map.get(id), authorized, ws,
    })
    : null
  // The assistant-stream tap: dsh publishes live chunks on
  // `agent/assistant-stream` (the v4 log holds only SETTLED events); the
  // sink relays each frame verbatim as `dsh_stream` through the same
  // channel and folds it into the session's reload baseline — registered
  // beside the session/event tap, `{ global: true }`
  // exactly as dsh's own session-controller registers it, so every agent's
  // publications reach the sink regardless of scope. No channel (no ws): no
  // turns run (followup refuses), so nothing is lost by not tapping.
  // WHY the cast: `Context` here is bare cordis; the `{ global }` option is
  // dsh-scope's augmentation of `on`, which this plugin's type graph never
  // imports (the session/event tap above types its handler `any` for the
  // same reason).
  const offStream = channel
    ? ctx.on('agent/assistant-stream', relayAssistantStream(channel, baselines), { global: true } as never)
    : null
  server.listen(PORT, () => {
    console.error(`[lore-driver] listening on ${PORT}`
      + (channel ? ' (+ /ws/events)' : ''))
  })
  ctx.on('dispose', () => {
    offTap()
    offObserve()
    offStream?.()
    channel?.close()
    server.close()
    disposeLoreTools()
  })
}

// ── The driver endpoints. apply() keeps only the server lifecycle; each
// endpoint is its own handler over the shared dsh session runner
// (resumeOrCreate / seedForkSession below), and the turn's own phases —
// activation restore, the restriction lifecycle, the session listener, the
// fault paths — are each their own unit. ──────────────────────────────────────

async function handleRequest(
  ctx: Context, map: SessionMap, tap: SessionEventTap, channel: EventsChannel | null,
  baselines: SessionStreamBaselines,
  req: http.IncomingMessage, res: http.ServerResponse,
): Promise<void> {
  const url = req.url ?? ''
  try {
    if (req.method === 'GET' && url.endsWith('/health')) {
      await health(req, res)
    } else if (req.method === 'GET' && url.startsWith('/capability')) {
      await capability(req, res)
    } else if (req.method === 'POST' && url.endsWith('/session-leaf')) {
      await sessionLeaf(ctx, map, channel, baselines, req, res)
    } else if (req.method === 'POST' && url.endsWith('/session-entries')) {
      await sessionEntries(ctx, map, baselines, req, res)
    } else if (req.method === 'POST' && url.endsWith('/approvals/resolve')) {
      await approvalsResolve(req, res, map, baselines)
    } else if (req.method === 'POST' && url.endsWith('/approvals')) {
      await approvalsPost(req, res)
    } else if (req.method === 'GET' && url.startsWith('/approvals')) {
      await approvalsList(req, res)
    } else if (req.method === 'POST' && url.endsWith('/followup')) {
      await followup(ctx, map, tap, channel, req, res)
    } else if (req.method === 'POST' && url.endsWith('/stop')) {
      await stop(ctx, map, req, res)
    } else {
      res.writeHead(404).end('not found')
    }
  } catch (err) {
    console.error(`[lore-driver] ${req.method} ${url} failed:`, err)
    if (!res.headersSent) res.writeHead(502).end(String(err))
    else res.end()
  }
}

function authorized(req: http.IncomingMessage): boolean {
  return Boolean(SECRET) && req.headers['x-driver-secret'] === SECRET
}

async function health(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
  if (!authorized(req)) {
    res.writeHead(401).end('unauthorized')
    return
  }
  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify({ status: 'ok' }))
}

// ── GET /capability?model= — the backend gates' ONE capability source.
// The backend's vision gate reads
// THIS reply (driver.client.agent_capability(model)) instead of re-resolving
// against the gateway itself; the numbers the turn needs never cross the wire
// at all — the turn handler resolves them from the same caps.ts source.
async function capability(
  req: http.IncomingMessage, res: http.ServerResponse,
): Promise<void> {
  if (!authorized(req)) {
    res.writeHead(401).end('unauthorized')
    return
  }
  const model = new URL(req.url ?? '/', 'http://lore-driver').searchParams.get('model') ?? ''
  if (!model) {
    res.writeHead(422).end('model is required')
    return
  }
  // The backend sends its gateway (admin panel, else env) the same way it
  // rides the turn payload; headers, so the key never lands in a URL.
  const gateway = gatewayOf(req.headers['x-ai-api-url'], req.headers['x-ai-api-key'])
  const caps = await resolveModelCaps(model, { gateway })
  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify({ vision: caps.vision }))
}

// ── The approval verdict endpoints — the backend's /api/chat/verdicts relay
// (the hold is driver-owned, so the user's verdict crosses THIS seam).
// GET /approvals?session_id= — the session's still-parked asks (reload
// re-render); POST /approvals — one verdict for one call id; POST
// /approvals/resolve — reject every ask of a session (session delete). The
// backend has ALREADY verified the poster owns the session before forwarding.

async function approvalsList(
  req: http.IncomingMessage, res: http.ServerResponse,
): Promise<void> {
  if (!authorized(req)) {
    res.writeHead(401).end('unauthorized')
    return
  }
  const query = new URL(req.url ?? '/', 'http://lore-driver')
  const sessionId = query.searchParams.get('session_id') ?? ''
  if (!sessionId) {
    res.writeHead(422).end('session_id is required')
    return
  }
  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify({ holds: pendingHoldsFor(sessionId) }))
}

async function approvalsPost(
  req: http.IncomingMessage, res: http.ServerResponse,
): Promise<void> {
  if (!authorized(req)) {
    res.writeHead(401).end('unauthorized')
    return
  }
  const body = JSON.parse(await readBody(req)) as Record<string, unknown>
  const callId = typeof body.call_id === 'string' ? body.call_id : ''
  const sessionId = typeof body.session_id === 'string' ? body.session_id : ''
  const action = typeof body.action === 'string' ? body.action : ''
  const toolName = typeof body.tool_name === 'string' ? body.tool_name : null
  const reason = typeof body.reason === 'string' ? body.reason : null
  if (!callId || !sessionId || !action) {
    res.writeHead(422).end('call_id, session_id and action are required')
    return
  }
  const status = resolveVerdict(callId, sessionId, action, toolName, reason)
  if (status === 409) {
    res.writeHead(409, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ detail: 'hold_expired' }))
    return
  }
  if (status === 404) {
    res.writeHead(404, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ detail: 'No held call for this call_id in this session' }))
    return
  }
  if (status === 422) {
    res.writeHead(422, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ detail: 'Unknown verdict action or missing tool_name' }))
    return
  }
  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify({ success: true }))
}

async function approvalsResolve(
  req: http.IncomingMessage, res: http.ServerResponse, map: SessionMap,
  baselines: SessionStreamBaselines,
): Promise<void> {
  if (!authorized(req)) {
    res.writeHead(401).end('unauthorized')
    return
  }
  const body = JSON.parse(await readBody(req)) as Record<string, unknown>
  const sessionId = typeof body.session_id === 'string' ? body.session_id : ''
  if (!sessionId) {
    res.writeHead(422).end('session_id is required')
    return
  }
  const reason = typeof body.reason === 'string' && body.reason ? body.reason : 'session_closed'
  const resolved = resolveSessionAsks(sessionId, reason)
  // The session-delete forward also prunes the identity map (the session's
  // dsh session is gone with it — the map entry would leak forever) and the
  // STANDING tool-ctx half (one registration per session, gone
  // with the session). The stream fold goes with the same dsh id.
  const dshId = map.get(sessionId)
  clearSessionToolCtx(dshId)
  baselines.forget(dshId)
  map.delete(sessionId)
  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify({ resolved }))
}

/** The model a fork seeds its session with: the route the parent last
 * requested at or before the branch point (its `request/header` — the same
 * fact dsh's titler reads), else '' — no fallback model (the value a fresh
 * install already forks with today). The next turn re-selects its own model
 * either way. */
export function forkModel(events: readonly unknown[], seq: number): string {
  for (let i = Math.min(seq, events.length - 1); i >= 0; i -= 1) {
    const ev = events[i] as { type?: string; data?: { header?: { config?: { model?: unknown } } } }
    const model = ev?.type === 'request/header' ? ev.data?.header?.config?.model : undefined
    if (typeof model === 'string' && model) return model
  }
  return ''
}

/** Shared dsh session runner, fork half: build dsh's OWN fork seed for the
 * boundary and flush a NEW session seeded with it (the create→flush→dispose
 * lifecycle a fork ride shares with the turn runner's create path). `parent`
 * is the session the seed was read from; `current` is the session the map and
 * the live subscriptions point at now — they differ when an earlier branch is
 * continued. */
async function seedForkSession(
  ctx: Context, map: SessionMap, channel: EventsChannel | null,
  baselines: SessionStreamBaselines,
  loreId: string, freshId: string, current: string, parent: string,
  events: unknown[], seq: number,
): Promise<void> {
  // buildForkSeed (dsh-session — the builder dsh's own SessionController.fork
  // uses): rows [0..seq], one `session/end-seed` inherited-cut marker at
  // seq + 1, synthetic `forked` closers for an open tail (a `turn/end`
  // boundary is already balanced — none). The inherited cut is the boundary
  // seq + 1, NOT the seed length: the marker and any closers are the child's
  // own setup rows, not inherited history.
  const seed = buildForkSeed(events as never, SessionSeq(seq))
  const handle = await ctx.agents.create({
    sessionId: SessionId(freshId as never),
    meta: {
      cwd: process.cwd(),
      parentSession: SessionId(parent as never),
      isSeeded: true,
    },
    inheritedEventCount: (seq + 1) as never,
    seed: seed as never,
    agentOptions: { provider: PROVIDER, model: forkModel(events, seq) },
    setup: (agentCtx: Context) => {
      agentCtx.systemPrompt.section({ name: 'lore-turn', order: 0, text: '', complete: true })
    },
  })
  try {
    await ctx.sessions.flush(handle.agent.session)
  } finally {
    await handle.dispose()
  }
  // The displaced id's stream fold is unreachable the moment the map moves
  // (a snapshot keys by the CURRENT dsh id) and nothing else deletes it —
  // dsh's own consumer drops a fold on agent/disposed, which a forked-away
  // id never reaches. Forget it with the move or one fold leaks per fork for
  // the process's lifetime.
  if (current !== freshId) baselines.forget(current)
  map.set(loreId, freshId)
  // The map moved; the live subscriptions follow it (the ws-events ARCH): the
  // re-keyed table + re-ack let the forked turn's frames stream to the
  // existing subscriber, whose dedup anchor re-hooks at the BOUNDARY seq —
  // the seed retains the parent prefix seqs, so without the re-ack the
  // subscriber would drop the whole forked turn as "already delivered". The
  // anchor is the boundary, never the marker's seq: the marker and closers
  // past the boundary are the child's own setup, and the fresh session's
  // next live event continues after them.
  // channel null (no ws) ⇒ no repoint: a later subscribe resolves the fresh
  // id by itself.
  channel?.repoint(loreId, current, freshId, seq)
}

/** The one read path over a dsh session log: open a read handle, take every
 * committed event, close. A missing session rejects with `session "<id>" not
 * found` — callers that tolerate that miss catch it themselves. */
async function readSessionEvents(ctx: Context, dshId: string): Promise<unknown[]> {
  const persistence = ctx.get('sessionPersistence')
  if (!persistence) throw new Error('session persistence is not configured')
  const handle = await persistence.open(SessionId(dshId as never), 'read')
  try {
    const { events } = await handle.read(0, undefined)
    return events as unknown[]
  } finally {
    await handle.close()
  }
}

// ── POST /session-entries — the RELOAD half of the relay.
// The transcript lives in the dsh session log; this replays it through the
// SAME mapEvent the live listener uses, grouped per turn. A session the
// driver has never seen returns an EMPTY turn list — an unknown session is
// not a failure, it is a thread with no dsh history (a pre-dsh thread, or one
// whose first turn has not run). `since_seq` (optional) is the resync
// boundary: the last frame seq the caller holds, mints included — see
// entries.ts for the filtering contract.
async function sessionEntries(
  ctx: Context, map: SessionMap, baselines: SessionStreamBaselines,
  req: http.IncomingMessage, res: http.ServerResponse,
): Promise<void> {
  if (!authorized(req)) {
    res.writeHead(401).end('unauthorized')
    return
  }
  const body = JSON.parse(await readBody(req)) as { session_id?: unknown; since_seq?: unknown }
  const loreId = typeof body.session_id === 'string' ? body.session_id : ''
  if (!loreId) {
    res.writeHead(422).end('session_id is required')
    return
  }
  // Any finite number is valid — mint seqs are fractional, so an integer-only
  // gate would make callers under-resync and re-receive held frames (a
  // duplicate Match in the browser assembler).
  if (body.since_seq !== undefined
    && (typeof body.since_seq !== 'number' || !Number.isFinite(body.since_seq))) {
    res.writeHead(422).end('since_seq must be a finite number')
    return
  }
  const sinceSeq = body.since_seq === undefined ? undefined : body.since_seq as number
  let events: unknown[] = []
  try {
    events = await readSessionEvents(ctx, map.get(loreId) as string)
  } catch (err) {
    // The one tolerated miss: a session that was never created. Anything else
    // is a READ FAILURE and must surface — the backend renders an explicit
    // error, never an empty thread (DriverTimelineUnavailable's INVARIANT).
    if (!(err instanceof Error && /not found/.test(err.message))) throw err
  }
  // The open turn's streamed-text baseline: the live-stream fold of the
  // session's CURRENT dsh id (stream-baselines.ts) — attached to the open
  // turn only, and only while an attempt is active (entries.ts's gate).
  const turns = projectSessionEntries(
    events as any[], sinceSeq, baselines.snapshot(map.get(loreId)))
  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify(turns))
}

// ── POST /session-leaf — the fork-branch-point primitive.
// `seq` (the dsh log seq of a `turn/end` row — the DRIVER's own id, read by
// the backend off the parent row's stamp): seed a NEW dsh session through
// that row via dsh's buildForkSeed (the prefix rows, the inherited-cut
// marker, forked closers for an open tail) and repoint the mapping (a fork's
// seed retains the parent seqs, so entry ids stay stable). `seq` null: root
// fork — a fresh empty session under a new id (the next entry is a ROOT
// sibling). `source` (optional, the dsh session id stamped beside the seq):
// the log the seq belongs to. Absent ⇒ the current session's log (rows
// stamped before the pair existed).
export async function sessionLeaf(
  ctx: Context, map: SessionMap, channel: EventsChannel | null,
  baselines: SessionStreamBaselines,
  req: http.IncomingMessage, res: http.ServerResponse,
): Promise<void> {
  if (!authorized(req)) {
    res.writeHead(401).end('unauthorized')
    return
  }
  const body = JSON.parse(await readBody(req)) as {
    session_id?: unknown; seq?: unknown; source?: unknown
  }
  const loreId = typeof body.session_id === 'string' ? body.session_id : ''
  if (!loreId || body.seq === undefined) {
    res.writeHead(422).end('session_id and seq are required')
    return
  }
  // # INVARIANT(corruption): a `source` must be this chat's own dsh session —
  // the lore id itself or a fork spelled `${loreId}~…`.
  // Why: the seed becomes the model's history; a foreign log would hand one
  // chat another chat's conversation.
  const source = body.source === undefined || body.source === null ? null : body.source
  if (source !== null
    && (typeof source !== 'string' || (source !== loreId && !source.startsWith(`${loreId}~`)))) {
    res.writeHead(422).end('source is not a session of this chat')
    return
  }
  const current = map.get(loreId)
  // # INVARIANT(corruption): the seq resolves in the log it was stamped from.
  // Why: dsh seqs are per-session and a root fork restarts them at 0, so an
  // abandoned branch's seq can equal a turn/end in the current log — resolving
  // it there seeds the old branch's continuation with the NEW branch's history.
  const parent = source ?? current
  const freshId = `${loreId}~f${randomUUID().slice(0, 8)}`
  if (process.env.LORE_DRIVER_DEBUG_EVENTS) {
    console.error(`[lore-driver] session-leaf lore=${loreId} dsh=${current} ` +
      `seq=${JSON.stringify(body.seq)}`)
  }

  if (body.seq === null) {
    // The displaced id's fold goes with the map move — seedForkSession's own
    // rule (the root fork moves the map without seeding).
    if (current !== freshId) baselines.forget(current)
    map.set(loreId, freshId)
    // Root fork: same repoint, tail null — the fresh log is EMPTY (the
    // subscriber's dedup anchor resets to "nothing delivered").
    channel?.repoint(loreId, current, freshId, null)
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: true }))
    return
  }
  const seq = Number(body.seq)
  // Non-negative: -1 is lastTurnEndSeq's "no completed turn" sentinel, so an
  // admitted negative could no-op against an empty log instead of refusing.
  if (!Number.isInteger(seq) || seq < 0) {
    res.writeHead(422).end('seq must be a non-negative integer or null')
    return
  }

  let events: unknown[]
  try {
    events = await readSessionEvents(ctx, parent)
  } catch (err) {
    // A missing SOURCE log is an unresolvable branch point, not a server
    // failure; a missing current log keeps its old path (it throws).
    if (parent === current || !(err instanceof Error && /not found/.test(err.message))) throw err
    console.error(`[lore-driver] leaf UNRESOLVABLE lore=${loreId} source=${parent} missing`)
    res.writeHead(422).end('branch point not resolvable in session')
    return
  }
  // No-op at the live tail: the backend's seam A names the parent turn on
  // EVERY turn, linear ones included — forking there would re-seed the
  // session per turn and kill in-place history + pressure compaction (see
  // lastTurnEndSeq's INVARIANT). Fork only on a REAL branch move — the live
  // tail is the CURRENT session's, so another source always forks.
  if (parent === current && seq === lastTurnEndSeq(events as unknown as any[])) {
    if (process.env.LORE_DRIVER_DEBUG_EVENTS) {
      console.error(`[lore-driver] leaf no-op (live tail) lore=${loreId} dsh=${current}`)
    }
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: true }))
    return
  }
  // A read v4 log is indexed by seq (the format's contiguity contract —
  // "sequence numbers stay contiguous", dsh session types.ts — pinned on a
  // real recorded log in test/driver.test.ts), so the boundary row is
  // addressed BY SEQ: a forkable branch point is a row whose position IS its
  // seq and whose kind is `turn/end` (the same guard shape as dsh's own
  // SessionController.fork). Anything else — a stale stamp, a
  // non-boundary row's seq, a seq past the log, garbage — is unresolvable,
  // and a fork must never seed to a boundary it cannot prove.
  const boundary = events[seq] as { seq?: number; type?: string } | undefined
  if (!boundary || boundary.seq !== seq || boundary.type !== 'turn/end') {
    // Unconditional: an unresolvable branch point is the failure this
    // endpoint exists to refuse — the numbers must be readable without a
    // debug env flip.
    console.error(`[lore-driver] leaf UNRESOLVABLE lore=${loreId} dsh=${parent} ` +
      `seq=${seq} turnEnds=${(events as unknown as any[]).filter((e) => e.type === 'turn/end').length} events=${events.length}`)
    res.writeHead(422).end('branch point not resolvable in session')
    return
  }
  if (process.env.LORE_DRIVER_DEBUG_EVENTS) {
    console.error(`[lore-driver] REAL FORK lore=${loreId} ${parent} -> ${freshId} ` +
      `seq=${seq} events=${events.length}`)
  }
  await seedForkSession(ctx, map, channel, baselines, loreId, freshId, current, parent, events, seq)
  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify({ ok: true }))
}

/** Shared dsh session runner, turn half: resume the persisted session or
 * create it on the first turn (resume-or-create from the plugin header). */
async function resumeOrCreate(
  ctx: Context, dshId: string, agentOptions: Record<string, unknown>,
  setup: (agentCtx: Context) => void,
): Promise<{ agent: any; dispose(): Promise<void> }> {
  try {
    return await ctx.agents.resume({
      resumeSessionId: SessionId(dshId as never),
      agentOptions,
      setup,
    })
  } catch (err) {
    // The resume contract (dsh-session-persistence, the pinned sha): ONLY a
    // session that was NEVER created rejects with `session "<id>" not found`
    // (SessionPersistenceNotFoundError). Anything else — a corrupt or
    // unreadable session log, a failed format migration — must NOT silently
    // degrade to a fresh session under the same id (the user would see a
    // memoryless agent as if nothing was lost); re-throw so the turn surfaces
    // the failure.
    if (err instanceof Error && /not found/.test(err.message)) {
      return await ctx.agents.create({
        sessionId: SessionId(dshId as never),
        meta: { cwd: process.cwd() },
        agentOptions,
        setup,
      })
    }
    throw err
  }
}

/** The served-toolset filter's activation half: the store is the dsh session
 * log itself — every `skill` load that delivered a body, intersected with the
 * CURRENT payload's skills (a deleted skill doc drops out). An absent or
 * unreadable session (first turn, fork race) degrades to NO activation —
 * core only. */
async function restoreActivation(
  ctx: Context, dshId: string, skills: ResolvedSkill[],
): Promise<Set<string>> {
  try {
    if (skills.length) {
      const events = await readSessionEvents(ctx, dshId)
      const activated = restoreActivatedSkills(events as unknown as any[], skills)
      console.error(`[lore-skills] restore: ${Array.isArray(events) ? events.length : '?'} events `
        + `→ ${activated.size} activated (${[...activated].join(',') || '-'})`)
      return activated
    }
  } catch (err) {
    // no session yet / unreadable log — core only
    console.error(`[lore-skills] restore failed (core only): ${String(err)}`)
  }
  return new Set<string>()
}

/** The restriction lifecycle: attached in the agent-scoped setup (hide from
 * prompt AND reject execution — the SERVED half of the filter), re-applied
 * mid-turn on a successful `skill` load, disposed at turn end. `activated` is
 * held BY REFERENCE — the session listener adds to that same set and calls
 * apply() to lift the freshly activated pack. */
function makeSkillRestriction(
  servedNames: Set<string>, skills: ResolvedSkill[], activated: Set<string>,
): { apply(): void; attach(agentCtx: Context): void; dispose(): void } {
  let release: (() => void) | null = null
  let agentToolsCtx: Context | null = null
  const apply = (): void => {
    release?.()
    release = null
    const deny = restrictDenyNames(
      registeredLoreToolNames(), servedNames, skills, activated)
    // The deny list is the filter's other observable — the exact complement
    // of the active set in `registered`: the inactive packs AND every
    // registered proxy this turn does not serve (`registered` is a
    // process-lifetime union across sessions), and never a tool an activated
    // skill freed — including one an inactive skill still lists.
    console.error(`[lore-skills] deny ${deny.length}: ${deny.join(',') || '-'}`)
    if (agentToolsCtx && deny.length) {
      release = agentToolsCtx.tools.restrict({ deny })
    }
  }
  return {
    apply,
    attach(agentCtx: Context) {
      agentToolsCtx = agentCtx
      apply()
    },
    dispose() {
      try {
        release?.()
      } catch {
        // best-effort — the agent scope may already be torn down
      }
      release = null
    },
  }
}

/** The session-projection seam the context-usage read needs (duck-typed: a
 * composition without the service passes undefined, and the tests fake it). */
export interface SessionProjectionsSeam {
  snapshot(session: unknown): { values: Record<string, unknown> }
}

/**
 * The per-turn context_usage frame off the driving session's `contextPressure`
 * projection — the harness's own occupancy accounting (uncached + cache
 * read/write of the last request, plus the surface's signed movement since
 * that sample), NOT a local sum of the raw usage chunk. Why the local sum was
 * deleted: a raw usage chunk is protocol-shaped, not occupancy — the old
 * route subtracted cache reads out of `inputTokens`, and pi-ai folds
 * reasoning tokens into output usage — so with prompt caching or reasoning
 * on, the local sum is a fraction (or a distortion) of real occupancy and
 * can DECREASE as the conversation grows.
 *
 * # ARCH: read the projection's WIRE VIEW (snapshot), never `stateOf` — the
 *   raw state has no `projectedTokens`; it is derived only in the wire view.
 *   Numerator: `projectedTokens ?? pressureTokens` — the projected figure is
 *   the only one that reacts to a compaction (which reports no usage of its
 *   own). Denominator: the driver's OWN gateway resolution (caps.ts) is the
 *   authority; the projection's `contextWindow` is the fallback when the
 *   resolution carries no number (unserved id → defaultContextWindow shows up
 *   here). Null when nothing is known — the caller then emits nothing.
 */
export function contextUsageFrame(
  projections: SessionProjectionsSeam | undefined,
  session: unknown,
  cap: number,
): Record<string, unknown> | null {
  if (!projections) return null
  let values: Record<string, unknown> | undefined
  try {
    values = projections.snapshot(session)?.values
  } catch (err) {
    // Loud degrade, never a mid-turn throw: the turn's outcome is already on
    // the wire and must not convert to an error over a gauge read.
    console.error('[lore-driver] contextPressure snapshot failed:', err)
    return null
  }
  const pressure = values?.contextPressure
  if (!pressure || typeof pressure !== 'object') return null
  if (typeof pressure.pressureTokens !== 'number') return null
  const used = typeof pressure.projectedTokens === 'number'
    ? pressure.projectedTokens
    : pressure.pressureTokens
  const window = cap > 0 ? cap : pressure.contextWindow
  return typeof window === 'number' && window > 0
    ? { type: 'context_usage', used, cap: window }
    : null
}

/** The ONE cancel arm: POST /stop cancels the live driver-owned turn HERE —
 * one semantics (`kind: 'user'`), one call site. Deferred into .then so a SYNC
 * throw from cancel cannot escape the event handler as an uncaughtException
 * (the trailing .catch then covers both sync and async rejections). */
function cancelAgentTurn(handle: { agent: any }): void {
  void Promise.resolve()
    .then(() => handle.agent.cancel({ kind: 'user' } as never))
    .catch(() => {})
}

/** dsh session id → the driver-owned turn in flight: one turn
 * per session — /followup refuses a busy session, /stop routes into the
 * cancel arm. */
const followupTurns = new Map<string, { cancel(): void }>()

/** Everything the driver-owned turn needs: payload parse, capability resolve,
 * model selection, the skills lifecycle, the restriction, tool registration,
 * the setup closure and the user-content prep. The /followup handler owns the
 * wire (channel pushes + the background turn); the prep stays separate from it
 * so the turn's identity/skills/tools setup is its own unit. */
async function prepareTurnDrive(ctx: Context, map: SessionMap, body: Record<string, any>) {
  const loreId = typeof body.session_id === 'string' && body.session_id
    ? body.session_id
    : `lore-${randomUUID()}`
  // The model comes only from the turn: the backend resolves body.model →
  // the session row → admin CHAT_MODEL and refuses a model-less turn itself;
  // a body that still names none dies here in the named refusal
  // (turnConfigProblem) — never runs on a guess (plan
  // component-wiring-not-settings step 3).
  const model = typeof body.model === 'string' && body.model ? body.model : ''
  // The gateway rides the turn (admin panel, else the backend's env): the
  // admin always wins, and the harness env is only the fallback.
  const gateway = gatewayOf(body.ai_api_url, body.ai_api_key)
  const problem = turnConfigProblem(model, gateway)
  if (problem) throw new TurnConfigError(map.get(loreId), problem)
  const route = loreRoute(gateway.base)
  // The title model rides the turn (admin CHAT_TITLE_MODEL, else the backend's
  // CHAT_MODEL — the backend folds the fallback); applied to the titler entry
  // in buildUserContent, after the catalog declares the model.
  const titleModel = (typeof body.title_model === 'string' ? body.title_model : '').trim()
  // The turn's time ground (backend-owned bytes: user timezone, `[chat started]`
  // root anchor, root-fork stability — completions_turn._turn_time_stamps).
  // Kept per turn; the setup's pre-step listener emits them — see ARCH in
  // time-stamps.ts.
  const timeStamps = parseTimeStamps(body.time_stamps)
  // The per-turn reasoning effort, in the GATEWAY's spelling:
  // null = Default — no explicit selection, NO reasoning_effort field on the
  // wire, and the provider's own default applies. The backend sends the field
  // only when the session row carries one, already validated against the
  // model's advertised list (and nameable in pi-ai — the picker's twin
  // filter). The selection below translates the spelling to the pi-ai level
  // KEY the adapter declares (identity for pi-ai's own names, `none` → `off`).
  const effort = typeof body.reasoning_effort === 'string' && body.reasoning_effort
    ? body.reasoning_effort
    : null
  // The driver resolves the model's capability itself: the gateway
  // /v1/models numbers and /v1/capabilities levels do not arrive in the
  // payload. An unserved id or an unreadable gateway → no numbers — the
  // composition's named defaults apply (defaultContextWindow in
  // cordis.patch.yml; dsh's own maxTokens), and cap=0 makes contextUsageFrame
  // fall back to the projection's own window.
  const caps = await resolveModelCaps(model, { gateway })
  const cap = caps.contextWindow ?? 0
  const maxOut = caps.maxOutputTokens ?? Number.NaN
  console.error(`[lore-caps] turn model=${model} known=${caps.known} `
    + `vision=${caps.vision} window=${caps.contextWindow ?? '-'} `
    + `maxOut=${caps.maxOutputTokens ?? '-'} `
    + `efforts=${caps.effortLevels === null ? '-' : JSON.stringify(caps.effortLevels)}`)
  const dshId = map.get(loreId)
  const agentOptions = { provider: PROVIDER, model }

  // The per-turn model selection:
  // dsh's own seam re-applies {provider, model, reasoningEffort} to every
  // step's request config and CLEARS any inherited effort when the selection
  // defines none — so Default sends no effort field (the provider default
  // applies) and a previously explicit effort never leaks into a later
  // Default turn (the agent handle is per-turn; the selection is rebuilt
  // from the CURRENT body). Installed in setup: setup completes before the
  // first followup, so the value reaches the FIRST step of the turn.
  // `agentOptions` keeps its {provider, model} runtime shape — the effort
  // rides the selection only.
  // An untranslatable spelling is sent VERBATIM with a loud log — the adapter
  // then refuses it (UNSUPPORTED_REASONING_EFFORT) rather than the turn
  // silently degrading to Default.
  let piEffort: string | null = null
  if (effort !== null) {
    piEffort = piAiEffortKey(effort)
    if (piEffort === null) {
      console.error(`[lore-driver] reasoning effort "${effort}" has no pi-ai level `
        + '— sending verbatim; the adapter refuses undeclared levels')
      piEffort = effort
    }
  }
  const selection: ModelSelectionRef = {
    current: {
      provider: PROVIDER,
      model,
      ...(piEffort ? { reasoningEffort: ReasoningEffortId(piEffort) } : {}),
    },
    assembled: undefined,
  }

  // ── The served-toolset filter (activation half: restoreActivation above).
  // The payload's skills field is the RAW lookup
  // (the backend parses NOTHING);
  // parsing + overlay + off-switch + the served gate happen in
  // resolveSkillCatalog, against the pinned harness's grammar.
  const catalogWire: SkillCatalogWire = (body.skills && typeof body.skills === 'object'
    && !Array.isArray(body.skills)) ? body.skills as SkillCatalogWire : { project: [], shipped: [], tombstones: [] }
  const servedNames = new Set<string>(
    (Array.isArray(body.tools) ? body.tools : [])
      .map((e: any) => String(e?.function?.name ?? '')),
  )
  const skills: ResolvedSkill[] = resolveSkillCatalog(
    catalogWire, catalogServedNames(servedNames))
  const activated = await restoreActivation(ctx, dshId, skills)
  const coreNames = computeCoreTools(servedNames, skills)
  // ONE active formula, shared with restrictDenyNames (the INVARIANT there):
  // the log and the enforcement must partition `registered` identically.
  const activeNames = computeActiveTools(servedNames, skills, activated)
  console.error(
    `[lore-skills] active tools resolved ${JSON.stringify(
      skillsDelta(servedNames, coreNames, activeNames))}`)

  // The restriction lifecycle (makeSkillRestriction above): attached in the
  // agent-scoped setup, re-applied mid-turn on a successful `skill` load,
  // disposed at turn end.
  const restriction = makeSkillRestriction(servedNames, skills, activated)

  const state = newTurnMapState()
  // The tool-call identity bridge: everything the
  // proxies and the map frames need to speak Lore's session identity. The
  // derived sets come from the PAYLOAD (single backend source), and the
  // registration is read-only first.
  const mutating = new Set<string>(Array.isArray(body.mutating_tools) ? body.mutating_tools : [])
  const turnToolCtx = {
    loreSessionId: loreId,
    messageId: String(body.assistant_msg_id ?? ''),
    agentKey: String(body.agent_key ?? ''),
    applyMode: String(body.apply_mode ?? 'confirm'),
    document_id: typeof body.document_id === 'string' && body.document_id
      ? body.document_id : null,
    mutating,
    holdable: new Set<string>(Array.isArray(body.holdable_tools) ? body.holdable_tools : []),
    regionTools: new Set<string>(Array.isArray(body.region_tools) ? body.region_tools : []),
    region: (body.region && typeof body.region === 'object')
      ? body.region as Record<string, unknown> : null,
  }
  // The split view: the standing half is registered once per
  // session; the per-request half is stamped by each driver-owned turn.
  const standingCtx = {
    loreSessionId: turnToolCtx.loreSessionId,
    agentKey: turnToolCtx.agentKey,
    mutating: turnToolCtx.mutating,
    holdable: turnToolCtx.holdable,
    regionTools: turnToolCtx.regionTools,
  }
  const requestCtx = {
    messageId: turnToolCtx.messageId,
    applyMode: turnToolCtx.applyMode,
    document_id: turnToolCtx.document_id,
    region: turnToolCtx.region,
  }

  // The lore-skills provider: dsh's registry + tool-skill own the
  // catalog and the model-facing `skill` loader; every resolved skill carries
  // its body from the same payload snapshot that built the catalog — no
  // Tool-API round trip on the load path, so no stale-index race and no
  // error envelope to propagate.
  const skillProvider = skills.length ? loreSkillProvider(skills) : null

  const setup = (agentCtx: Context) => {
    // The system prompt is built ONCE per turn by Lore's prep layer and
    // handed over — a COMPLETE section is the exact whole prompt; nothing
    // harness-side contributes (identity, persona, runtime contexts). The
    // skills catalog is NOT concatenated here: dsh's tool-skill
    // publishes it as its own durable session message.
    agentCtx.systemPrompt.section({
      name: 'lore-turn',
      order: 0,
      text: assembleTurnPrompt(body.system_prompt),
      complete: true,
    })
    installModelSelection(agentCtx, selection)
    // The time ground rides its own context message on the turn's FIRST step
    // (see ARCH in time-stamps.ts). Scoped to THIS turn's agent, disposed with
    // it in finish(): no per-session stamp state. DELEGATE first (context alone is not a veto),
    // then append after the downstream decision's admitted messages.
    agentCtx.on('agent/pre-step', async ({ step }, next): Promise<PreStepDecision> => {
      const downstream = await next()
      const ours = timeStampMessage(timeStamps, step)
      if (ours === undefined || downstream.kind !== 'enter') return downstream
      return { ...downstream, messages: [...downstream.messages, ours] }
    })
    restriction.attach(agentCtx)
    if (skillProvider) {
      // Scoped to THIS agent: the provider serves exactly this turn's payload
      // skills and is disposed with the agent scope. The registration needs a
      // context that DECLARES the skills inject — the agent scope itself does
      // not inherit the plugin's inject list, so an injected sub-scope carries
      // it (the same cordis shape ApprovalService uses for systemPrompt).
      agentCtx.inject(['skills'], (scoped) => {
        scoped.skills.registerProvider(() => skillProvider)
      })
    }
  }

  registerLoreTools(ctx, body.tools, true, mutating)

  // The multimodal turn prompt (shared by both drivers): dsh's saveImage
  // normalizes pixels (decode→scale→encode, up to seconds per image).
  // Everything not deliverable degrades to a text block naming the loss
  // (promptContent) — never a silent drop.
  const saveImage: SaveImage = (input) => {
    // Risk 2: no guard of our own beyond naming the seam — a missing store
    // becomes that part's `[attachment not delivered: …]` reason.
    const store = ctx.get('attachments')
    if (!store) throw new Error('the durable attachment service is not composed')
    return store.saveImage({
      ...input,
      // No media-type list here by design: forward the
      // type the data URI declares and let dsh sniff the bytes.
      mediaType: input.mediaType as ImageMediaType,
    })
  }
  const buildUserContent = async () => {
    await ensureAgentKey(ctx.get('credentials') as CredentialsSeam | undefined, gateway.key)
    // The per-turn admin config (web search has no off state; the provider is
    // always pinned and the credential rides even when empty — the loud
    // no-key failure). Awaited before the agent runs, so an admin change
    // reaches THIS turn's calls.
    await ensureWebSearch(
      ctx.get('configEditor') as ConfigEditorSeam | undefined,
      ctx.get('credentials') as CredentialsSeam | undefined,
      typeof body.web_search_provider === 'string' ? body.web_search_provider : '',
      typeof body.web_search_credential === 'string' ? body.web_search_credential : '',
    )
    const blocks = await prepareUserContent(
      body.prompt, saveImage, ctx.get('settings'), model, cap, maxOut,
      caps.effortLevels, route)
    // After the chat model's own upsert (which carries the route scaffold);
    // the titler runs after the turn's first request, so this lands first.
    if (titleModel && titleModel !== model) {
      await ensureTitleEntry(ctx.get('settings') as SettingsSeam, titleModel, route)
    }
    await ensureTitleConfig(
      ctx.get('configEditor') as ConfigEditorSeam | undefined, titleModel)
    return blocks
  }

  return {
    loreId, dshId, model, cap, caps, agentOptions,
    skills, activated, restriction, state,
    turnToolCtx, standingCtx, requestCtx,
    setup, buildUserContent,
  }
}

// ── POST /followup — the driver-owned turn: the request STARTS the turn and
// returns `accepted`; frames flow on /ws/events (map.ts's mints through the
// standing channel, the turn-lifecycle frames pushed below). Statelessness
// between turns is unchanged (the header ARCH): resume-or-create → followup →
// quiescence → flush → dispose, once per followup — the standing thing is the
// consumer's SUBSCRIPTION, not an agent handle the driver keeps alive.
async function followup(
  ctx: Context, map: SessionMap, tap: SessionEventTap,
  channel: EventsChannel | null,
  req: http.IncomingMessage, res: http.ServerResponse,
): Promise<void> {
  if (!authorized(req)) {
    res.writeHead(401).end('unauthorized')
    return
  }
  if (!channel) {
    // No silent degradation: without the standing channel the driver-owned
    // turn has NO delivery path — refuse explicitly rather than run a turn
    // whose frames reach nobody.
    res.writeHead(503).end('event channel unavailable')
    return
  }
  const body = JSON.parse(await readBody(req)) as Record<string, any>
  let drive: Awaited<ReturnType<typeof prepareTurnDrive>>
  try {
    drive = await prepareTurnDrive(ctx, map, body)
  } catch (err) {
    if (!(err instanceof TurnConfigError)) throw err
    // Accepted with only the refusal and the close: the backend already bound
    // the turn, so the error frame reaches the person and finalizes the row.
    // WHY model_update first: it is the frame that opens the bound turn on the
    // backend channel (the normal path's first push) — without it the error
    // relays live only, the row stays empty and the turn lock is never freed.
    console.error(`[lore-driver] turn refused: ${err.message}`)
    channel.push(err.dshId, { type: 'model_update', model: typeof body.model === 'string' ? body.model : '' })
    channel.push(err.dshId, { type: 'error', message: err.message })
    channel.push(err.dshId, { type: 'turn_closed' })
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ accepted: true, dsh_session_id: err.dshId, lore_session_id: body.session_id }))
    return
  }
  const { dshId, loreId, model, cap } = drive
  // One turn per session — the driver-side serialize. A second followup
  // while one runs is refused, never queued;
  // the caller retries after the turn ends.
  if (followupTurns.has(dshId)) {
    res.writeHead(409, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ detail: 'turn_in_progress' }))
    return
  }
  const handle = await resumeOrCreate(ctx, dshId, drive.agentOptions, drive.setup)
  const agent = handle.agent
  followupTurns.set(dshId, { cancel: () => cancelAgentTurn(handle) })
  // The tool-ctx split: the standing half upserts per session;
  // the request half is THIS turn's stamp, cleared at its end.
  setSessionToolCtx(dshId, drive.standingCtx)
  stampTurnRequestCtx(dshId, drive.requestCtx)
  // The turn watcher ('watch' phase — runs BEFORE the channel's mapped
  // frames): the turn-end context_usage must precede the terminal relay on
  // the wire (the backend captures it at turn finalization; after the terminal
  // frame it is too late).
  // Mid-turn `skill` activation lifting the pack restriction live: a load
  // that DELIVERED a body adds the skill to `activated` (held by reference
  // by the restriction) and re-applies the deny list, so the pack's tools are
  // callable in the SAME turn the model loaded the skill.
  const pendingSkillCalls = new Map<string, string>()
  const skillNames = new Set(drive.skills.map((s) => s.name))
  const offWatch = tap.subscribe((session: any, ev: any) => {
    if (String(session?.id ?? '') !== dshId) return
    const skillCall = skillLoadCallName(ev)
    if (skillCall) pendingSkillCalls.set(skillCall.callId, skillCall.name)
    const activatedSkill = skillLoadResultName(ev, pendingSkillCalls)
    if (activatedSkill && skillNames.has(activatedSkill)
        && !drive.activated.has(activatedSkill)) {
      drive.activated.add(activatedSkill)
      console.error(`[lore-skills] activated ${activatedSkill} — lifting its pack restriction`)
      drive.restriction.apply()
    }
    if (ev?.type !== 'turn/end') return
    // The operator's one line for a failed turn (the chat blanks an AUTH
    // message; the raw gateway reason survives only here and in the session
    // JSONL). Per-turn tap, so a failure logs once — not once per browser.
    const failed = turnFailureLine(ev, { loreId, dshId, model })
    if (failed) console.error(failed)
    drive.state.finished = true
    const cu = contextUsageFrame(
      (ctx as any).get?.('sessionProjections'), agent.session, cap)
    if (cu) channel.push(dshId, cu)
  }, 'watch')

  const finish = async () => {
    followupTurns.delete(dshId)
    offWatch()
    clearTurnRequestCtx(dshId)
    drive.restriction.dispose()
    try {
      await handle.dispose()
    } catch {
      // best-effort teardown — the turn outcome is already on the channel
    }
  }

  try {
    // A resumed session settles before the prompt lands: whenIdle, THEN
    // content, THEN followup.
    await agent.whenIdle()
    const content = await drive.buildUserContent()
    channel.push(dshId, { type: 'model_update', model })
    agent.followup(createUserMessage({
      content,
      source: { kind: 'user' },
    }))
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ accepted: true, dsh_session_id: dshId, lore_session_id: loreId }))
  } catch (err) {
    console.error('[lore-driver] followup failed:', err)
    await finish()
    throw err // → 502 via handleRequest, like every other start failure
  }

  // The driver-owned turn: nobody holds a request open — the turn runs to
  // its end (or /stop's cancel), flushes, and cleans itself up.
  void (async () => {
    try {
      await agent.whenIdle()
      await ctx.sessions.flush(agent.session)
      if (!drive.state.finished) {
        channel.push(dshId, { type: 'error', message: 'The turn ended without a terminal event.' })
      }
    } catch (err) {
      console.error('[lore-driver] followup turn failed:', err)
      channel.push(dshId, { type: 'error', message: `Harness turn failed: ${String(err)}` })
    } finally {
      await finish()
      // The transport terminal: a driver-owned turn has no stream whose end
      // signals the turn's close, so THIS explicit push is that terminal —
      // pushed after every mapped frame of the turn (the terminal relay and
      // its halt mint included) and after the not-finished error above.
      // Best-effort: a closed channel loses it, and the backend re-mints on
      // reconnect — the resync replay for a turn/end lost mid-gap, the
      // owed-close re-mint for a turn that had already ENDED live before
      // the gap (driver.channel._resync_all).
      try { channel.push(dshId, { type: 'turn_closed' }) } catch { /* gone — the resync re-mints */ }
    }
  })()
}

// ── POST /stop — cancel the session's driver-owned turn: routes into the ONE
// cancel arm (cancelAgentTurn) — one cancel semantics, one call site, no
// second cancel-key machinery. Idempotent: a session with no turn in flight
// answers {stopped:false} — a turn that ended a moment ago is not an error.
async function stop(
  ctx: Context, map: SessionMap,
  req: http.IncomingMessage, res: http.ServerResponse,
): Promise<void> {
  if (!authorized(req)) {
    res.writeHead(401).end('unauthorized')
    return
  }
  const body = JSON.parse(await readBody(req)) as { session_id?: unknown }
  const loreId = typeof body.session_id === 'string' ? body.session_id : ''
  if (!loreId) {
    res.writeHead(422).end('session_id is required')
    return
  }
  const entry = followupTurns.get(map.get(loreId))
  if (entry) entry.cancel()
  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify({ stopped: Boolean(entry), session_id: loreId }))
}

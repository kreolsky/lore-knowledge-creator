/**
 * The driver-owned turn — the config gate (turnConfigProblem), the
 * multimodal prompt blocks, the catalog-backed user content, the turn
 * drive (prepareTurnDrive) and the POST /followup + POST /stop handlers.
 * index.ts re-exports the public surface.
 */

import { randomUUID } from 'node:crypto'
import http from 'node:http'
import type { Context } from '@deepseek-ai/cordis'
import { createUserMessage, ReasoningEffortId } from '@deepseek-ai/dsh-llm'
import type { ContentBlock } from '@deepseek-ai/dsh-llm'
import { installModelSelection } from '@deepseek-ai/dsh-agent'
import type { ModelSelectionRef } from '@deepseek-ai/dsh-agent'
import type { PreStepDecision } from '@deepseek-ai/dsh-agent'
import type { ImageAttachmentRef, ImageMediaType } from '@deepseek-ai/dsh-attachment'

import { newTurnMapState, turnFailureLine } from './map.ts'
import {
  gatewayOf, piAiEffortKey,
  resolveModelCaps,
  type Gateway, type ModelApi,
} from './caps.ts'
import { parseTimeStamps, timeStampMessage } from './time-stamps.ts'
import type { EventsChannel, SessionEventTap } from './ws-events.ts'
import {
  assembleTurnPrompt, catalogServedNames, computeCoreTools, computeActiveTools, loreSkillProvider,
  resolveSkillCatalog, restrictDenyNames, skillLoadCallName,
  skillLoadResultName, skillsDelta, type ResolvedSkill, type SkillCatalogWire,
} from './skills.ts'
import {
  clearTurnRequestCtx, registerLoreTools,
  registeredLoreToolNames, setSessionToolCtx,
  stampTurnRequestCtx,
} from './tools.ts'
import {
  AGENT_KEY_REF, ROUTE_DEFAULT_CONTEXT_WINDOW, PROVIDER,
  ensureAgentKey, ensureModelEntry, ensureTitleConfig, ensureTitleEntry, ensureWebSearch,
  type ConfigEditorSeam, type CredentialsSeam, type LoreRouteScaffold, type SettingsSeam,
} from './ensure.ts'
import { resumeOrCreate, restoreActivation } from './sessions.ts'
import { authorized, readBody } from './http.ts'

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
 * the harness holds no fallback of its own. */
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

/** The `lore` route scaffold (openai-completions): null when there is no
 * base (no fallback — the turn is refused, never pretends to a route). */
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

/** The `anthropic` route scaffold (anthropic-messages): the gateway base with
 * a trailing `/v1` or `/v1/` removed — the Anthropic SDK inside pi-ai appends
 * its own request paths, so the URL it gets is the bare origin
 * (`AI_API_URL=http://api.ai.gray/v1` → `http://api.ai.gray`). NO `api` and
 * NO route `compat`: the route name is the pi-ai INSTALLED-CATALOG provider
 * id, so the catalog's shared `anthropic-messages` protocol applies to every
 * model the catalog does not describe, the catalog's own compat inherits
 * onto the ids it does, and compat a model needs beyond that is per model —
 * the router's `/v1/models` entry carries it (caps.ts), never the route.
 * `defaultContextWindow` because the router leaves such a model bare and
 * dsh's own schema default (262144) would silently change the
 * context_usage denominator. Null on an empty base, same refusal path as
 * loreRoute. */
export function anthropicRoute(baseURL: string): LoreRouteScaffold | null {
  if (!baseURL) return null
  return {
    apiKeyEnv: AGENT_KEY_REF,
    baseURL: baseURL.replace(/\/v1\/?$/, ''),
    defaultContextWindow: ROUTE_DEFAULT_CONTEXT_WINDOW,
  }
}

/** The route names this composition serves. `anthropic` is the pi-ai
 * installed-catalog provider id — that name is load-bearing: a configured
 * model spreads the installed entry of the same id under it (`...base` in
 * llm-pi-ai's catalog), the only way the Claude flags dsh withholds from
 * configuration (supportsMidConvoEffort, supportsMidConvoSystemMessages,
 * supportsMidConvoToolChanges) reach a model. */
export type RouteName = 'lore' | 'anthropic'

/** ONE map: the caps api (the router's `/v1/models` `api` field) names the
 * route every site uses — agentOptions, the selection, the title pair, the
 * catalog upsert. */
export const ROUTE_BY_API: Readonly<Record<ModelApi, RouteName>> = {
  'openai-completions': 'lore',
  'anthropic-messages': 'anthropic',
}

/** The scaffold builder for a route name — the one place a route name
 * becomes its scaffold. */
function routeScaffoldFor(provider: RouteName, baseURL: string): LoreRouteScaffold | null {
  return provider === 'anthropic' ? anthropicRoute(baseURL) : loreRoute(baseURL)
}

/** The user-message content for one turn: the mapped prompt blocks plus the
 * catalog upsert that must land BEFORE the followup. EVERY turn upserts (the
 * entry's `maxTokens` is the router-sourced output cap — see
 * ensureModelEntry); on image turns the same entry also declares image
 * capability (pi-ai's `[text]` input default would otherwise make the harness
 * refuse the attachment before it is attached); whenever the gateway
 * advertises effort levels the entry also carries the `reasoningEfforts`
 * declaration (an undeclared level dies in dsh before network I/O); the
 * entry lands under `provider`'s route section with its scaffold, and its
 * `compat` rides verbatim when the router's entry carries one. A missing
 * settings seam fails LOUD on every turn — the cap is not optional
 * metadata. */
export async function prepareUserContent(
  prompt: unknown,
  saveImage: SaveImage,
  settings: SettingsSeam | undefined,
  model: string,
  contextWindow: number,
  maxOutputTokens: number,
  effortLevels: readonly string[] | null,
  scaffold: LoreRouteScaffold | null = null,
  compat: Record<string, unknown> | null = null,
  provider: string = PROVIDER,
): Promise<ContentBlock[]> {
  const { blocks, images } = await promptContent(prompt, saveImage)
  if (!settings) {
    throw new Error('the settings service is not composed — cannot declare the model catalog entry')
  }
  await ensureModelEntry(
    settings, model, contextWindow, maxOutputTokens, images > 0, effortLevels,
    false, scaffold, compat, provider,
  )
  return blocks
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
 * cancel arm. Exported because it IS the liveness the log read branches on
 * (readSessionEvents): the crash-closer gate keys on exactly this registry,
 * and the tests name that semantics by setting a key directly. */
export const followupTurns = new Map<string, { cancel(): void }>()

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
  // (turnConfigProblem) — never runs on a guess.
  const model = typeof body.model === 'string' && body.model ? body.model : ''
  // The gateway rides the turn (admin panel, else the backend's env): the
  // admin always wins, and the harness env is only the fallback.
  const gateway = gatewayOf(body.ai_api_url, body.ai_api_key)
  const problem = turnConfigProblem(model, gateway)
  if (problem) throw new TurnConfigError(map.get(loreId), problem)
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
    + `maxOut=${caps.maxOutputTokens ?? '-'} api=${caps.api} `
    + `efforts=${caps.effortLevels === null ? '-' : JSON.stringify(caps.effortLevels)}`)
  // The ROUTE comes off the caps api (the router names the wire protocol per
  // model): one map, one scaffold builder per route — every site below reads
  // `provider`, never a literal.
  const provider = ROUTE_BY_API[caps.api]
  const route = routeScaffoldFor(provider, gateway.base)
  const dshId = map.get(loreId)
  const agentOptions = { provider, model }

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
      provider,
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
      caps.effortLevels, route, caps.compat, provider)
    // After the chat model's own upsert (which carries the route scaffold);
    // the titler runs after the turn's first request, so this lands first.
    // The TITLE model resolves its OWN route off the same gateway — its api
    // may differ from the turn model's (an openai-protocol chat with an
    // anthropic-protocol titler, or the reverse).
    let titleProvider: RouteName = provider
    if (titleModel && titleModel !== model) {
      const titleCaps = await resolveModelCaps(titleModel, { gateway })
      titleProvider = ROUTE_BY_API[titleCaps.api]
      await ensureTitleEntry(
        ctx.get('settings') as SettingsSeam, titleModel, titleProvider,
        routeScaffoldFor(titleProvider, gateway.base), titleCaps.compat)
    }
    await ensureTitleConfig(
      ctx.get('configEditor') as ConfigEditorSeam | undefined, titleModel,
      titleProvider)
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
export async function followup(
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
      // Best-effort: a closed channel loses it, and the replay covers the
      // loss — a resync of a NON-live session carries a seq-anchored
      // turn_closed per closed turn (entries.ts's `terminals`); a live
      // session's newer turn supersedes the lost push instead.
      try { channel.push(dshId, { type: 'turn_closed' }) } catch { /* gone — the replay covers */ }
    }
  })()
}

// ── POST /stop — cancel the session's driver-owned turn: routes into the ONE
// cancel arm (cancelAgentTurn) — one cancel semantics, one call site, no
// second cancel-key machinery. Idempotent: a session with no turn in flight
// answers {stopped:false} — a turn that ended a moment ago is not an error.
export async function stop(
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

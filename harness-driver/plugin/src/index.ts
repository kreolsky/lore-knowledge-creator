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
import { SessionId } from '@deepseek-ai/dsh-session'
import { createUserMessage, ReasoningEffortId } from '@deepseek-ai/dsh-llm'
import type { ContentBlock } from '@deepseek-ai/dsh-llm'
import { installModelSelection } from '@deepseek-ai/dsh-agent'
import type { ModelSelectionRef } from '@deepseek-ai/dsh-agent'
import type { ImageAttachmentRef, ImageMediaType } from '@deepseek-ai/dsh-attachment'

import { mapEvent, newTurnMapState } from './map.ts'
import { loreEvent } from '../../lore-conversation/src/lore-events.ts'
import { lastTurnEndSeq, turnEndIndex } from './leaf.ts'
import {
  assertNonReasoningTitleModel, piAiEffortKey,
  reasoningEffortsDeclaration, resolveModelCaps,
} from './caps.ts'
import { projectSessionEntries } from './entries.ts'
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
import { WEB_SEARCH_PIN } from './web-search/pin.ts'
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

/** The roster entry this line serves. CRASH ON MISSING CONFIG
 * (coding-constraints): the harness composition declares exactly ONE model
 * and there is no fallback entry — cordis.patch.yml resolves the model
 * straight from the var, so a deploy that forgot it would run the agent on
 * the wrong brain with no error. Called at apply() so
 * the container dies loudly at boot rather than at the first user's turn. */
export function harnessModel(): string {
  const model = (process.env.LORE_HARNESS_MODEL || '').trim()
  if (!model) {
    throw new Error(
      'LORE_HARNESS_MODEL is required: the harness line serves one roster entry '
      + 'and has no fallback model (set HARNESS_MODEL in the deploy env).')
  }
  return model
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

// ── The per-turn image-capability upsert (same plan, Decision 4/5).
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

/** The settings seam the upsert talks to (ctx.settings; duck-typed for tests). */
export interface SettingsSeam {
  get(ns: unknown): unknown
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
 * number. `ifAbsent` (the boot path) writes ONLY when no entry exists: a
 * committed entry stays untouched instead of being downgraded to id-only
 * until the model's next turn. */
export async function ensureModelEntry(
  settings: SettingsSeam, model: string, contextWindow: number,
  maxOutputTokens: number, images: boolean,
  effortLevels: readonly string[] | null,
  ifAbsent = false,
): Promise<void> {
  const run = settingsWriteChain.then(async () => {
    // Re-read INSIDE the chain: a concurrent turn's committed entry must be
    // the base this upsert computes from.
    const section = settings.get(LLM_NS) as {
      providers?: Record<string, { models?: CatalogModelEntry[] }>
    } | undefined
    const models: CatalogModelEntry[] = section?.providers?.[ROUTE]?.models ?? []
    const committed = models.find((m) => m.id === model)
    // The boot write exists to land ids the static layer does not declare —
    // checked inside the chain so a concurrent first turn is the writer, not
    // a race between both.
    if (ifAbsent && committed) return
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
    // Skip when the committed entry already carries every value this turn
    // would write. The image modality is only REQUIRED on image turns: a
    // text-only turn leaves an image-capable entry untouched (no rewrite, no
    // strip) — the next image turn re-declares it before its own followup.
    if (committed && committed.contextWindow === cap
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
      providers: { [ROUTE]: { models: [...models.filter((m) => m.id !== model), entry] } },
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
): Promise<ContentBlock[]> {
  const { blocks, images } = await promptContent(prompt, saveImage)
  if (!settings) {
    throw new Error('the settings service is not composed — cannot declare the model catalog entry')
  }
  await ensureModelEntry(settings, model, contextWindow, maxOutputTokens, images > 0, effortLevels)
  return blocks
}

// ── The boot catalog upsert + the title-model gate.
//
// # ARCH: pi-ai refuses a model id absent from the route's catalog
//   (UNKNOWN_MODEL), and the ONLY static entries are the bootstrap harness
//   model in cordis.patch.yml — so CHAT_TITLE_MODEL (and a changed harness
//   model) must land in the settings section at boot, id-only and ifAbsent:
//   a committed entry (a previous turn already filled its caps/efforts) is
//   left untouched, never downgraded to id-only; the caps numbers and effort
//   levels land on each model's first turn (ensureModelEntry). The title
//   model additionally passes the crash-on-config gate
//   (assertNonReasoningTitleModel): the titler names no effort, and on this
//   openai-format route "no field" is the provider's default — a reasoner
//   would think on every title and there is no per-purpose knob to say
//   "don't think", so the config is refused at boot (same class as
//   harnessModel). A gateway unreadable at boot warns and skips the gate;
//   the id-only writes still land.

/** Retry a settings write against an as-yet-unregistered namespace: the
 * `llm-pi-ai` namespace registers when that plugin applies, which can land
 * after this boot task's first attempt (plugin configs and applies are not
 * ordered relative to a network-gated task). Bounded to ~2s; other errors
 * propagate. */
async function settingsWriteRetry(write: () => Promise<void>): Promise<void> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      await write()
      return
    } catch (err) {
      if (attempt < 20 && err instanceof Error && /not registered/.test(err.message)) {
        await new Promise((resolve) => setTimeout(resolve, 100))
        continue
      }
      throw err
    }
  }
}

/** The boot task itself — scoped to the settings service via ctx.inject (the
 * plugin applies BEFORE the settings plugin, so the service does not exist at
 * apply() time; the inject callback fires when it does). A gate failure exits
 * the process (crash-on-config), a write failure only logs: the turns then
 * surface the missing entry loudly at their own upsert. */
function bootUpsertModels(ctx: Context): void {
  ctx.inject(['settings'], (sctx: Context) => {
    const settings = (sctx as { get?: (name: string) => unknown }).get?.('settings') as
      | SettingsSeam
      | undefined
    if (!settings) return // the scope guarantees it; typed for the test seam
    const titleModel = (process.env.CHAT_TITLE_MODEL || process.env.CHAT_MODEL || '').trim()
    void (async () => {
      if (titleModel) {
        try {
          const caps = await resolveModelCaps(titleModel)
          if (!caps.known) {
            console.error(`[lore-caps] cannot resolve CHAT_TITLE_MODEL "${titleModel}" against `
              + 'the gateway (unreadable or not served) — not gating (warning, not a crash)')
          } else {
            assertNonReasoningTitleModel(caps, titleModel)
          }
        } catch (err) {
          console.error('[lore-caps] CHAT_TITLE_MODEL boot gate failed — exiting:', err)
          process.exit(1)
        }
      }
      const harness = harnessModel()
      try {
        await settingsWriteRetry(() =>
          ensureModelEntry(settings, harness, Number.NaN, Number.NaN, false, null, true))
        if (titleModel && titleModel !== harness) {
          await settingsWriteRetry(() =>
            ensureModelEntry(settings, titleModel, Number.NaN, Number.NaN, false, null, true))
        }
        console.error(`[lore-caps] boot upsert: `
          + `${[...new Set([harness, titleModel].filter(Boolean))].join(', ')} `
          + `→ llm-pi-ai.providers.${ROUTE}.models`)
      } catch (err) {
        console.error('[lore-caps] boot upsert write failed — the entry lands on the model\'s first turn instead:', err)
      }
    })()
  })
}

export function apply(ctx: Context): void {
  harnessModel() // boot-time config gate — see the crash-on-missing-config WHY above
  bootUpsertModels(ctx) // id-only entries + the title-model gate — see the ARCH block above
  const map = new SessionMap(process.env.DSH_HOME)
  // The ONE session-event tap: apply() holds the single
  // ctx.on('session/event') subscription; the standing WS channel and the
  // per-turn watch subscribe through the tap, so every relay sink sees the
  // same events in the same synchronous order.
  const tap = createSessionEventTap()
  const offTap = ctx.on('session/event', (session: any, ev: any) => tap.emit(session, ev))
  const server = http.createServer((req, res) => { void handleRequest(ctx, map, tap, channel, req, res) })
  // The approval bridge (step 7): dsh's approval/request waterfall is answered
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
  // The assistant-stream tap (0.1.5): dsh publishes live chunks on
  // `agent/assistant-stream` (the v3 log holds only SETTLED events); the
  // sink relays each frame verbatim as `dsh_stream` through the same
  // channel — registered beside the session/event tap, `{ global: true }`
  // exactly as dsh's own session-controller registers it, so every agent's
  // publications reach the sink regardless of scope. No channel (no ws): no
  // turns run (followup refuses), so nothing is lost by not tapping.
  // WHY the cast: `Context` here is bare cordis; the `{ global }` option is
  // dsh-scope's augmentation of `on`, which this plugin's type graph never
  // imports (the session/event tap above types its handler `any` for the
  // same reason).
  const offStream = channel
    ? ctx.on('agent/assistant-stream', relayAssistantStream(channel), { global: true } as never)
    : null
  server.listen(PORT, () => {
    console.error(`[lore-driver] listening on ${PORT}`
      + (channel ? ' (+ /ws/events)' : ''))
  })
  ctx.on('dispose', () => {
    offTap()
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
  req: http.IncomingMessage, res: http.ServerResponse,
): Promise<void> {
  const url = req.url ?? ''
  try {
    if (req.method === 'GET' && url.endsWith('/health')) {
      await health(req, res)
    } else if (req.method === 'GET' && url.startsWith('/capability')) {
      await capability(req, res)
    } else if (req.method === 'POST' && url.endsWith('/session-leaf')) {
      await sessionLeaf(ctx, map, channel, req, res)
    } else if (req.method === 'POST' && url.endsWith('/session-entries')) {
      await sessionEntries(ctx, map, req, res)
    } else if (req.method === 'POST' && url.endsWith('/approvals/resolve')) {
      await approvalsResolve(req, res, map)
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

// ── GET /capability?model= — the backend gates' ONE capability source (plan
// collapse-the-editor-harness-layer step 4). The backend's vision gate reads
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
  const caps = await resolveModelCaps(model)
  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify({ vision: caps.vision }))
}

// ── The approval verdict endpoints — the backend's /api/chat/verdicts relay
// (step 7: the hold is driver-owned, so the user's verdict crosses THIS seam).
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
  // STANDING tool-ctx half (plan step 3: one registration per session, gone
  // with the session).
  clearSessionToolCtx(map.get(sessionId))
  map.delete(sessionId)
  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify({ resolved }))
}

/** Shared dsh session runner, fork half: seed a NEW session with a prefix and
 * flush it (the create→flush→dispose lifecycle a fork ride shares with the
 * turn runner's create path). `parent` is the session the seed was read from;
 * `current` is the session the map and the live subscriptions point at now —
 * they differ when an earlier branch is continued. */
async function seedForkSession(
  ctx: Context, map: SessionMap, channel: EventsChannel | null,
  loreId: string, freshId: string, current: string, parent: string, seed: any[],
): Promise<void> {
  const handle = await ctx.agents.create({
    sessionId: SessionId(freshId as never),
    meta: {
      cwd: process.cwd(),
      parentSession: SessionId(parent as never),
      isSeeded: true,
    },
    inheritedEventCount: seed.length as never,
    seed: seed as never,
    agentOptions: { provider: PROVIDER, model: harnessModel() },
    setup: (agentCtx: Context) => {
      agentCtx.systemPrompt.section({ name: 'lore-turn', order: 0, text: '', complete: true })
    },
  })
  try {
    await ctx.sessions.flush(handle.agent.session)
  } finally {
    await handle.dispose()
  }
  map.set(loreId, freshId)
  // The map moved; the live subscriptions follow it (the ws-events ARCH): the
  // re-keyed table + re-ack let the forked turn's frames stream to the
  // existing subscriber, whose dedup anchor re-hooks at the seed's boundary
  // seq (the seed retains the parent prefix seqs, so without the tail the
  // subscriber would drop the whole forked turn as "already delivered").
  // channel null (no ws) ⇒ no repoint: a later subscribe resolves the fresh
  // id by itself.
  channel?.repoint(loreId, current, freshId, seed.at(-1)?.seq ?? null)
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
  ctx: Context, map: SessionMap,
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
  const turns = projectSessionEntries(events as any[], sinceSeq)
  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify(turns))
}

// ── POST /session-leaf — the fork-branch-point primitive.
// `seq` (the dsh log seq of a `turn/end` row — the DRIVER's own id, read by
// the backend off the parent row's stamp): seed a NEW dsh session with the
// balanced prefix through that row and repoint the mapping (a fork's seed
// retains the parent seqs, so entry ids stay stable). `seq` null: root fork —
// a fresh empty session under a new id (the next entry is a ROOT sibling).
// `source` (optional, the dsh session id stamped beside the seq): the log the
// seq belongs to. Absent ⇒ the current session's log (rows stamped before the
// pair existed).
export async function sessionLeaf(
  ctx: Context, map: SessionMap, channel: EventsChannel | null,
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
  const boundaryIndex = turnEndIndex(events as unknown as any[], seq)
  if (boundaryIndex < 0) {
    // Unconditional: an unresolvable branch point is the failure this
    // endpoint exists to refuse — the numbers must be readable without a
    // debug env flip.
    console.error(`[lore-driver] leaf UNRESOLVABLE lore=${loreId} dsh=${parent} ` +
      `seq=${seq} turnEnds=${(events as unknown as any[]).filter((e) => e.type === 'turn/end').length} events=${events.length}`)
    res.writeHead(422).end('branch point not resolvable in session')
    return
  }
  // # INVARIANT: the seed is a POSITIONAL prefix (rows in log order through
  // the boundary row), never a seq filter. Why: dsh logs carry companion
  // rows without `seq` (tool-call-chunks, text-chunks batches) between the
  // seq'd events; `ev.seq <= boundary` drops them mid-prefix, the seed
  // stops being "contiguous from seq 0" (the create() contract), and the
  // fork silently materializes EMPTY — observed live as a fork file whose
  // only content was the driving turn, with the parent history gone.
  // turnEndIndex already answered the ROW, so slicing through it satisfies
  // the invariant by construction.
  const seed = (events as unknown as any[]).slice(0, boundaryIndex + 1)
  if (process.env.LORE_DRIVER_DEBUG_EVENTS) {
    console.error(`[lore-driver] REAL FORK lore=${loreId} ${parent} -> ${freshId} ` +
      `seq=${seq} seed=${seed.length}/${events.length}`)
  }
  await seedForkSession(ctx, map, channel, loreId, freshId, current, parent, seed)
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

/** dsh session id → the driver-owned turn in flight (plan step 3): one turn
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
  const model = typeof body.model === 'string' && body.model ? body.model : harnessModel()
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
  const caps = await resolveModelCaps(model)
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
  // The payload's skills field is the RAW lookup (plan
  // collapse-the-editor-harness-layer step 3: the backend parses NOTHING);
  // parsing + overlay + off-switch + the served gate happen in
  // resolveSkillCatalog, against the pinned harness's grammar.
  const catalogWire: SkillCatalogWire = (body.skills && typeof body.skills === 'object'
    && !Array.isArray(body.skills)) ? body.skills as SkillCatalogWire : { project: [], shipped: [], tombstones: [] }
  const servedNames = new Set<string>(
    (Array.isArray(body.tools) ? body.tools : [])
      .map((e: any) => String(e?.function?.name ?? '')),
  )
  const skills: ResolvedSkill[] = resolveSkillCatalog(
    catalogWire, catalogServedNames(servedNames, process.env[WEB_SEARCH_PIN] !== undefined))
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
  // registration is read-only first — mutating proxies are step 3.
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
  // The split view (Decision 12): the standing half is registered once per
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

  // The lore-skills provider (step 8, bodies in memory since step 3 of
  // collapse-the-editor-harness-layer): dsh's registry + tool-skill own the
  // catalog and the model-facing `skill` loader; every resolved skill carries
  // its body from the same payload snapshot that built the catalog — no
  // Tool-API round trip on the load path, so no stale-index race and no
  // error envelope to propagate.
  const skillProvider = skills.length ? loreSkillProvider(skills) : null

  const setup = (agentCtx: Context) => {
    // The system prompt is built ONCE per turn by Lore's prep layer and
    // handed over — a COMPLETE section is the exact whole prompt; nothing
    // harness-side contributes (identity, persona, runtime contexts). The
    // skills catalog is NOT concatenated here (step 8): dsh's tool-skill
    // publishes it as its own durable session message.
    agentCtx.systemPrompt.section({
      name: 'lore-turn',
      order: 0,
      text: assembleTurnPrompt(body.system_prompt),
      complete: true,
    })
    installModelSelection(agentCtx, selection)
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
      // No media-type list here by design (plan Decision 2): forward the
      // type the data URI declares and let dsh sniff the bytes.
      mediaType: input.mediaType as ImageMediaType,
    })
  }
  const buildUserContent = () => prepareUserContent(
    body.prompt, saveImage, ctx.get('settings'), model, cap, maxOut,
    caps.effortLevels)

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
  const drive = await prepareTurnDrive(ctx, map, body)
  const { dshId, loreId, model, cap } = drive
  // One turn per session — the driver-side serialize the pump's turn lock
  // used to own. A second followup while one runs is refused, never queued;
  // the caller retries after the turn ends.
  if (followupTurns.has(dshId)) {
    res.writeHead(409, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ detail: 'turn_in_progress' }))
    return
  }
  const handle = await resumeOrCreate(ctx, dshId, drive.agentOptions, drive.setup)
  const agent = handle.agent
  followupTurns.set(dshId, { cancel: () => cancelAgentTurn(handle) })
  // The tool-ctx split (Decision 12): the standing half upserts per session;
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
      // Best-effort: a closed channel loses it,
      // and the backend's resync replay re-mints the close on reconnect.
      try { channel.push(dshId, { type: 'turn_closed' }) } catch { /* gone — resync covers */ }
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

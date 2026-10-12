/**
 * One dsh agent per session — the live inbox that IS the chat queue, the
 * per-claim prep swap, the claim/status frames and the gated idle dispose.
 *
 * # ARCH: the queue is dsh's inbox. `/followup` stores the message's prep
 *   (the backend-built facts of ONE send) keyed by the dsh message id and
 *   calls `agent.followup`; dsh serializes the turns, holds the queue after an
 *   errored turn and keeps it across a `keepInbox` cancel. A claim
 *   (`agent/inbox/claimed`, synchronous, before the step's prompt assembly)
 *   swaps that message's prep in as the session's CURRENT prep and pushes
 *   `lore/claim` — the backend mints the row pair from it. Everything the
 *   step reads (selection, prompt section, time stamps, deny list, skills,
 *   tool ctx) is an indirection over the current prep, installed ONCE in the
 *   agent's setup: a closure over a first turn's values would run every later
 *   turn with them.
 */

import type { Context } from '@deepseek-ai/cordis'
import { createUserMessage, MessageId } from '@deepseek-ai/dsh-llm'
import type { ContentBlock } from '@deepseek-ai/dsh-llm'
import { installModelSelection } from '@deepseek-ai/dsh-agent'
import type { ModelSelection, ModelSelectionRef, PreStepDecision } from '@deepseek-ai/dsh-agent'

import { turnFailureLine } from './map.ts'
import { timeStampMessage } from './time-stamps.ts'
import type { EventsChannel, SessionEventTap } from './ws-events.ts'
import {
  LORE_SKILL_PROVIDER, loreSkillProvider, restrictDenyNames, skillLoadCallName,
  skillLoadResultName, type ResolvedSkill,
} from './skills.ts'
import {
  clearTurnRequestCtx, registeredLoreToolNames, setSessionToolCtx, stampTurnRequestCtx,
  type SessionToolCtx, type TurnRequestCtx,
} from './tools.ts'

/** The facts `lore/claim` carries back to the backend verbatim — the row
 * pair and the preamble are minted from THESE, never from the dsh message
 * (which holds the merged context images, not the user's own attachments). */
export interface ClaimFacts {
  user_msg_id: string
  assistant_msg_id: string
  user_text: string
  images: unknown[]
  sources: unknown[]
  warnings: unknown[]
}

/** One send's frozen facts — built by the backend at POST, applied at claim. */
export interface TurnPrep {
  loreId: string
  model: string
  /** The resolved context window (0 = unknown → the projection's own). */
  cap: number
  selection: ModelSelection
  systemPrompt: string
  timeStamps: string[]
  servedNames: Set<string>
  skills: ResolvedSkill[]
  /** Skill loads restored from the log at POST (unioned into the session's set). */
  activated: Set<string>
  standingCtx: SessionToolCtx
  requestCtx: TurnRequestCtx
  claim: ClaimFacts
}

export interface AgentHandle { agent: any; dispose(): Promise<void> }

/** What a live session agent needs from the plugin around it. */
export interface SessionAgentDeps {
  ctx: Context
  channel: EventsChannel
  tap: SessionEventTap
  open(dshId: string, agentOptions: Record<string, unknown>,
    setup: (agentCtx: Context) => void): Promise<AgentHandle>
  idleMs: number
  /** The context_usage frame at a turn's end (null = emit nothing). */
  contextUsage(session: unknown, cap: number): Record<string, unknown> | null
}

// WHY 10 minutes: a live agent holds its session's writer lock and its
// projections in memory; an idle chat releases them, and the next send pays
// one resume. Long enough that a conversation's pauses never re-resume.
export const AGENT_IDLE_DISPOSE_MS = 10 * 60_000

interface SessionAgent {
  dshId: string
  deps: SessionAgentDeps
  handle: AgentHandle
  agent: any
  /** Prep per pending dsh message id: stored before followup, gone at claim/discard. */
  preps: Map<string, TurnPrep>
  current: TurnPrep | null
  selection: ModelSelectionRef
  /** The session's activated skills — log restore at each POST + live loads. */
  activated: Set<string>
  /** A claim opened a turn whose `turn_closed` is not pushed yet. */
  turnOpen: boolean
  releaseRestriction: (() => void) | null
  agentCtx: Context | null
  invalidateSkills: (() => void) | null
  offWatch: () => void
  idleTimer: ReturnType<typeof setTimeout> | null
}

/** dsh session id → its live agent. Exported because it IS the liveness
 * the log read branches on (sessionAgentLive): the tests name that semantics
 * by setting a key directly. */
export const sessionAgents = new Map<string, SessionAgent>()
const live = sessionAgents
const creating = new Map<string, Promise<SessionAgent>>()
const disposing = new Map<string, Promise<void>>()
/** Requests (/followup, /inbox/discard) in flight per session — counted
 * BEFORE the agent lookup, so the idle dispose can never race one. */
const inflight = new Map<string, number>()

/** The session has a turn RUNNING — the log read's liveness (crash closers
 * and replayed terminals stay off a session that is streaming). */
export function sessionAgentLive(dshId: string): boolean {
  return live.get(dshId)?.agent?.status === 'running'
}

function hold(dshId: string): () => void {
  inflight.set(dshId, (inflight.get(dshId) ?? 0) + 1)
  let released = false
  return () => {
    if (released) return
    released = true
    const left = (inflight.get(dshId) ?? 1) - 1
    if (left > 0) {
      inflight.set(dshId, left)
      return
    }
    inflight.delete(dshId)
    const entry = live.get(dshId)
    if (entry && entry.agent.status === 'idle') scheduleIdleDispose(entry)
  }
}

/** The deny list off the CURRENT prep (no claim yet → nothing denied: no
 * step runs before the first claim). */
function applyRestriction(entry: SessionAgent): void {
  entry.releaseRestriction?.()
  entry.releaseRestriction = null
  const prep = entry.current
  if (!prep || !entry.agentCtx) return
  const deny = restrictDenyNames(
    registeredLoreToolNames(), prep.servedNames, prep.skills, entry.activated)
  console.error(`[lore-skills] deny ${deny.length}: ${deny.join(',') || '-'}`)
  if (deny.length) entry.releaseRestriction = entry.agentCtx.tools.restrict({ deny })
}

function closeOpenTurn(entry: SessionAgent): void {
  if (!entry.turnOpen) return
  entry.turnOpen = false
  clearTurnRequestCtx(entry.dshId)
  // The transport terminal, after every mapped frame of the turn (the halt
  // mint included). Best-effort: a closed channel loses it, and the replay
  // covers the loss for a session that is not running.
  try { entry.deps.channel.push(entry.dshId, { type: 'turn_closed' }) } catch { /* the replay covers */ }
}

function onClaim(entry: SessionAgent, message: { id: unknown }): void {
  // The previous turn's terminal precedes this claim on the wire, always.
  closeOpenTurn(entry)
  const messageId = String(message.id)
  const prep = entry.preps.get(messageId)
  entry.preps.delete(messageId)
  if (!prep) {
    // Unreachable by construction (the creation-time clear) — a message with
    // no facts must not run on guessed ones: stop it, keep the rest queued.
    console.error(`[lore-driver] claim without prep session=${entry.dshId} message=${messageId} — cancelling the turn`)
    cancelKeepingQueue(entry)
    return
  }
  entry.current = prep
  entry.selection.current = prep.selection
  for (const name of prep.activated) entry.activated.add(name)
  applyRestriction(entry)
  entry.invalidateSkills?.()
  setSessionToolCtx(entry.dshId, prep.standingCtx)
  stampTurnRequestCtx(entry.dshId, prep.requestCtx)
  entry.turnOpen = true
  entry.deps.channel.push(entry.dshId, {
    type: 'lore/claim', message_id: messageId, ...prep.claim, model: prep.model,
  })
  entry.deps.channel.push(entry.dshId, { type: 'model_update', model: prep.model })
}

function onStatus(entry: SessionAgent, status: string): void {
  // ARCH: turn status is dsh's own `agent/status`, relayed raw — the browser's
  // busy state and the reload's open-turn mark (GET /session-status) both read
  // it; Lore holds no admission state of its own.
  entry.deps.channel.push(entry.dshId, { type: 'dsh_status', status })
  if (status === 'running') {
    if (entry.idleTimer) clearTimeout(entry.idleTimer)
    entry.idleTimer = null
    return
  }
  void (async () => {
    try {
      await entry.deps.ctx.sessions.flush(entry.agent.session)
    } catch (err) {
      console.error(`[lore-driver] flush failed session=${entry.dshId}:`, err)
    }
    if (entry.turnOpen) {
      entry.deps.channel.push(entry.dshId, {
        type: 'error', message: 'The turn ended without a terminal event.',
      })
      closeOpenTurn(entry)
    }
    // INVARIANT: an idle transition never wakes the agent — after an errored
    // turn (or a parking stop) dsh holds the queue, and only the next send starts it.
    // Why: one model/key error must not run the whole queue into the same error.
    if (entry.agent.status === 'idle') scheduleIdleDispose(entry)
  })()
}

/** The agent-scoped setup, installed ONCE per agent: every value it hands dsh
 * reads the session's current prep at the moment dsh asks. */
function sessionSetup(entry: SessionAgent): (agentCtx: Context) => void {
  return (agentCtx: Context) => {
    entry.agentCtx = agentCtx
    // The system prompt is Lore's per-send prep, handed over whole: a
    // COMPLETE section is the exact prompt; nothing harness-side contributes.
    // The skills catalog is NOT concatenated here: dsh's tool-skill
    // publishes it as its own durable session message.
    agentCtx.systemPrompt.section({
      name: 'lore-turn',
      order: 0,
      text: () => entry.current?.systemPrompt ?? '',
      complete: true,
    })
    installModelSelection(agentCtx, entry.selection)
    // The time ground rides its own context message on each turn's FIRST
    // step (see ARCH in time-stamps.ts). DELEGATE first (context alone is not
    // a veto), then append after the downstream decision's admitted messages.
    agentCtx.on('agent/pre-step', async ({ step }: { step: number }, next: () => Promise<PreStepDecision>): Promise<PreStepDecision> => {
      const downstream = await next()
      const ours = timeStampMessage(entry.current?.timeStamps ?? [], step)
      if (ours === undefined || downstream.kind !== 'enter') return downstream
      return { ...downstream, messages: [...downstream.messages, ours] }
    })
    // The skills registration needs a context that DECLARES the skills
    // inject — the agent scope does not inherit the plugin's inject list, so
    // an injected sub-scope carries it (the same cordis shape ApprovalService
    // uses for systemPrompt). The provider serves the current prep's skills;
    // a claim invalidates the catalog cache.
    agentCtx.inject(['skills'], (scoped: any) => {
      scoped.skills.registerProvider((control: { invalidate(): void }) => {
        entry.invalidateSkills = () => control.invalidate()
        const skills = () => loreSkillProvider(entry.current?.skills ?? [])
        return {
          name: LORE_SKILL_PROVIDER,
          list: (options: any) => skills().list(options),
          get: (candidate: any, options: any) => skills().get(candidate, options),
        }
      })
    })
    const mine = (agent: { id?: unknown } | undefined) => String(agent?.id ?? '') === entry.dshId
    agentCtx.on('agent/inbox/claimed', ({ agent, message }: { agent: any; message: { id: unknown } }) => {
      if (mine(agent)) onClaim(entry, message)
    })
    agentCtx.on('agent/status', ({ agent, status }: { agent: any; status: string }) => {
      if (mine(agent)) onStatus(entry, status)
    })
  }
}

/** The session watcher ('watch' phase — BEFORE the channel's mapped frames):
 * mid-turn skill activation, and at a claimed turn's end the failure line,
 * the context_usage frame and the deferred terminal. */
function sessionWatch(entry: SessionAgent): (session: any, ev: any) => void {
  const pendingSkillCalls = new Map<string, string>()
  return (session, ev) => {
    if (String(session?.id ?? '') !== entry.dshId) return
    const prep = entry.current
    const skillCall = skillLoadCallName(ev)
    if (skillCall) pendingSkillCalls.set(skillCall.callId, skillCall.name)
    const activatedSkill = skillLoadResultName(ev, pendingSkillCalls)
    if (activatedSkill && prep?.skills.some((s) => s.name === activatedSkill)
        && !entry.activated.has(activatedSkill)) {
      entry.activated.add(activatedSkill)
      console.error(`[lore-skills] activated ${activatedSkill} — lifting its pack restriction`)
      applyRestriction(entry)
    }
    if (ev?.type !== 'turn/end' || !entry.turnOpen || !prep) return
    const failed = turnFailureLine(ev, { loreId: prep.loreId, dshId: entry.dshId, model: prep.model })
    if (failed) console.error(failed)
    const cu = entry.deps.contextUsage(entry.agent.session, prep.cap)
    if (cu) entry.deps.channel.push(entry.dshId, cu)
    // After this event's mapped frames (the tap fans out synchronously), and
    // before the next claim (which also closes first — onClaim).
    queueMicrotask(() => closeOpenTurn(entry))
  }
}

async function createSessionAgent(
  deps: SessionAgentDeps, dshId: string, agentOptions: Record<string, unknown>,
): Promise<SessionAgent> {
  // A disposing predecessor still holds the session's writer lock.
  await disposing.get(dshId)
  const entry = {
    dshId, deps, preps: new Map(), current: null,
    selection: {
      current: {
        provider: String(agentOptions.provider ?? ''), model: String(agentOptions.model ?? ''),
      },
      assembled: undefined,
    },
    activated: new Set<string>(), turnOpen: false, releaseRestriction: null,
    agentCtx: null, invalidateSkills: null, offWatch: () => {}, idleTimer: null,
  } as unknown as SessionAgent
  const handle = await deps.open(dshId, agentOptions, sessionSetup(entry))
  entry.handle = handle
  entry.agent = handle.agent
  await entry.agent.whenIdle()
  // INVARIANT(data-loss): a newly created agent starts with an EMPTY inbox —
  // a resumed log (harness restart) or a fork seed may still carry pending
  // inserts; they are cleared here, in the creation path, outside any log
  // observer. Why: a message queued before a restart has no prep and would
  // run with guessed facts.
  if (entry.agent.inbox.hasPending) entry.agent.inbox.clear()
  entry.offWatch = deps.tap.subscribe(sessionWatch(entry), 'watch')
  live.set(dshId, entry)
  return entry
}

async function liveAgent(
  deps: SessionAgentDeps, dshId: string, agentOptions: Record<string, unknown>,
): Promise<SessionAgent> {
  const ready = live.get(dshId)
  if (ready) return ready
  let pending = creating.get(dshId)
  if (!pending) {
    // Two concurrent first sends share ONE creation.
    pending = createSessionAgent(deps, dshId, agentOptions)
      .finally(() => creating.delete(dshId))
    creating.set(dshId, pending)
  }
  return await pending
}

function scheduleIdleDispose(entry: SessionAgent): void {
  if (entry.idleTimer) clearTimeout(entry.idleTimer)
  entry.idleTimer = setTimeout(() => { void tryDispose(entry) }, entry.deps.idleMs)
  entry.idleTimer.unref?.()
}

async function tryDispose(entry: SessionAgent): Promise<void> {
  entry.idleTimer = null
  const { dshId, agent } = entry
  // INVARIANT(data-loss): dispose only an idle agent with an empty inbox and
  // no request in flight — checked and unregistered synchronously. Why: a
  // graceful dispose durably DISCARDS the pending inbox.
  if (live.get(dshId) !== entry || (inflight.get(dshId) ?? 0) > 0
      || agent.status !== 'idle' || agent.inbox.hasPending) return
  live.delete(dshId)
  const done = (async () => {
    entry.offWatch()
    entry.releaseRestriction?.()
    try {
      await entry.handle.dispose()
    } catch (err) {
      console.error(`[lore-driver] agent dispose failed session=${dshId}:`, err)
    }
  })()
  disposing.set(dshId, done)
  await done
  if (disposing.get(dshId) === done) disposing.delete(dshId)
}

/** Queue one send: the prep is stored under the dsh message id BEFORE
 * followup (an idle agent claims synchronously inside it). */
export async function enqueueFollowup(
  deps: SessionAgentDeps, dshId: string, agentOptions: Record<string, unknown>,
  prep: TurnPrep, content: () => Promise<ContentBlock[]>,
): Promise<string> {
  const release = hold(dshId)
  try {
    const entry = await liveAgent(deps, dshId, agentOptions)
    const message = createUserMessage({ content: await content(), source: { kind: 'user' } })
    const messageId = String(message.id)
    entry.preps.set(messageId, prep)
    try {
      entry.agent.followup(message)
    } catch (err) {
      entry.preps.delete(messageId)
      throw err
    }
    return messageId
  } finally {
    release()
  }
}

/** ✕ on a queued message: false when it was claimed (or never queued) —
 * the claim splice and the transcript are the caller's feedback. */
export async function discardQueued(dshId: string, messageId: string): Promise<boolean> {
  const release = hold(dshId)
  try {
    const entry = live.get(dshId) ?? await creating.get(dshId)
    if (!entry) return false
    const removed: boolean = entry.agent.inbox.remove(MessageId(messageId))
    if (removed) entry.preps.delete(messageId)
    return removed
  } finally {
    release()
  }
}

/** The ONE cancel arm: abort the running turn and PARK the queue — the next
 * send starts it (no plugin-side wake). Deferred into .then so a SYNC throw
 * from cancel cannot escape the caller (a handler or a claim listener). */
function cancelKeepingQueue(entry: SessionAgent): void {
  void Promise.resolve()
    .then(() => entry.agent.cancel({ kind: 'user' }, { keepInbox: true }))
    .catch(() => {})
}

/** One queued message handed back to the composer by the person's Stop. */
export interface ReturnedSend { text: string; images: unknown[] }

/** POST /stop: `stopped` when a turn was running and is now being cancelled.
 * `returnQueue` (the person's Stop) also takes every queued message out of
 * the inbox and returns its text and images in send order, for the composer;
 * without it (the backend's deadline breach) the queue stays parked. */
export function stopSession(
  dshId: string, returnQueue = false,
): { stopped: boolean; returned: ReturnedSend[] } {
  const entry = live.get(dshId)
  if (!entry) return { stopped: false, returned: [] }
  const returned = returnQueue ? takeQueue(entry) : []
  const stopped = entry.agent.status === 'running'
  if (stopped) cancelKeepingQueue(entry)
  return { stopped, returned }
}

/** Empty the inbox in ONE splice (the chips leave with it) and return what
 * the person sent — the prep's own text and attachments, not the dsh message
 * (which holds the merged context images). Synchronous with the read, so no
 * claim can take a message between the two. */
function takeQueue(entry: SessionAgent): ReturnedSend[] {
  const queued = [...entry.agent.inbox.nextTurn] as Array<{ id: unknown }>
  if (queued.length === 0) return []
  entry.agent.inbox.clear()
  return queued.flatMap((message) => {
    const id = String(message.id)
    const prep = entry.preps.get(id)
    entry.preps.delete(id)
    return prep ? [{ text: prep.claim.user_text, images: prep.claim.images }] : []
  })
}

/** The live agent's status, its next-turn queue (the inbox projection's own
 * wire shape) and the seq that snapshot includes; no agent ⇒ idle, empty. */
export function sessionStatus(dshId: string): { status: string; queue: unknown[]; seq: number | null } {
  const entry = live.get(dshId)
  if (!entry) return { status: 'idle', queue: [], seq: null }
  return {
    status: entry.agent.status,
    queue: [...entry.agent.inbox.nextTurn],
    seq: Number(entry.agent.session.seq) - 1,
  }
}

/** Plugin teardown: stop the idle timers (the context owns the agents). */
export function disposeSessionAgents(): void {
  for (const entry of live.values()) {
    if (entry.idleTimer) clearTimeout(entry.idleTimer)
    entry.offWatch()
  }
  live.clear()
}

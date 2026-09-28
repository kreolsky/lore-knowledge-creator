/**
 * The Lore approval bridge — dsh's user-approval service, answered by Lore.
 *
 * # SYSTEM: harness-driver (approval half). The ask lives dsh-side
 *   (`ctx.approval.request` → the `approval/asked`/`approval/decided` audit
 *   pair on the session log, fail-closed outcomes); the ANSWERER is Lore's
 *   verdict surface: this bridge parks the question (keyed by the tool call
 *   id), the VerdictCard renders from the relayed `lore/verdict-ask` mint,
 *   and the user's POST /api/chat/verdicts reaches the parked question through
 *   the driver's own HTTP endpoints below (the backend relays after its
 *   session-access check — our RBAC is the answerer's gate).
 *
 * # ARCH: the hold is DRIVER-OWNED — the backend decision_store is deleted.
 *   Nothing parks
 *   inside a Tool-API request anymore: dsh's ApprovalService awaits this
 *   bridge's promise inside the agent's own turn, so the turn never ends and
 *   the model never re-reasons while the user decides.
 *
 * # INVARIANT(security): the driver endpoints speak ONLY to the Lore backend
 *   (the X-Driver-Secret every driver endpoint requires), and the backend has
 *   ALREADY verified the verdict poster owns the session before it forwards.
 *   The allow_session grant is minted from the PARKED ASK's record (never the
 *   request body), so a grant can only cover the tool the question was
 *   actually about — the asker's short-circuit then skips re-asking.
 *
 * # INVARIANT: an unanswered ask self-resolves. LORE_APPROVAL_MAX_S bounds one
 *   ask (a self-heal and user-patience bound, the old decision_store
 *   HOLD_MAX_S); the expiry trace answers the owning session's LATE verdict
 *   with an explicit 409 while a different session probing the same call id
 *   keeps the uniform 404 (no existence oracle).
 */

import type { Context } from '@deepseek-ai/cordis'
import type { ApprovalOutcome } from '@deepseek-ai/dsh-user-approval'

import type { TurnToolCtx } from './tools.ts'

/** One parked ask: everything the verdict endpoints need + the resolver. */
interface PendingAsk {
  loreSessionId: string
  toolName: string
  messageId: string
  resolve: (outcome: ApprovalOutcome) => void
  expiresAt: number
  timer: ReturnType<typeof setTimeout>
}

/** The bound on ONE ask (the backend's turn-deadline pause cap mirrors it). */
export const LORE_APPROVAL_MAX_S = Number(process.env.LORE_APPROVAL_MAX_S || 600)

/** The expiry-trace cap — a bound on the 409-answer memory, pruned on insert. */
const EXPIRED_TRACE_MAX = 256

/** callId → the parked ask (written SYNCHRONOUSLY at answerer entry). */
const pending = new Map<string, PendingAsk>()

/** callId → loreSessionId for asks that self-resolved past the bound (409 trace). */
const expired = new Map<string, string>()

/** callId → the reject words for the asker's result envelope (cleared on read). */
const rejectReasons = new Map<string, string>()

/** Session-scoped allow grants: `${loreSessionId} ${toolName}` — swept when
 * the session closes (resolveSessionAsks). */
const grants = new Set<string>()

/** The bridge's own ctx (set at registration; the asker reads ctx.approval). */
let bridgeCtx: Context | null = null

/** The dsh session id → turn ctx lookup (tools.ts's map, injected at
 * registration so this module never imports tools.ts's mutable singleton). */
let turnCtxFor: ((dshId: string) => TurnToolCtx | undefined) | null = null

// ─── Pure decisions (unit-testable without a harness boot) ───────────────────

/** The grant key for one (lore session, tool) pair. */
export function grantKey(loreSessionId: string, toolName: string): string {
  return `${loreSessionId} ${toolName}`
}

/** Map OUR verdict vocabulary onto dsh's outcomes ('allowed-once' is the only
 * grant; allow_session grants at the bridge and resolves allowed-once). */
export function verdictOutcome(action: string): ApprovalOutcome | null {
  if (action === 'allow_once' || action === 'allow_session') return 'allowed-once'
  if (action === 'reject') return 'rejected'
  return null
}

/** What POST /approvals answers for one (call_id, session_id, action) triple:
 * 200 (and the outcome to resolve), 404 (no such ask — the uniform refusal),
 * 409 (the OWNING session's ask expired), or 422 (a malformed action).
 * `toolName` is cross-checked against the ASK's record, never trusted. */
export function postVerdictAnswer(
  callId: string, sessionId: string, action: string, toolName: string | null,
  state: {
    pending: Map<string, PendingAsk>
    expired: Map<string, string>
  } = { pending, expired },
): { status: number; outcome?: ApprovalOutcome; grant?: boolean } {
  if (verdictOutcome(action) === null) return { status: 422 }
  const ask = state.pending.get(bareAskKey(callId))
  if (!ask) {
    // Owned-expiry oracle (session-bound, read only after the backend's
    // session-access check): a late verdict on the owning session's expired
    // ask fails with a reason, not the mute 404.
    if (state.expired.get(bareAskKey(callId)) === sessionId) return { status: 409 }
    return { status: 404 }
  }
  if (ask.loreSessionId !== sessionId) return { status: 404 }
  if (action === 'allow_session') {
    // The grant target is the ASK's tool (the card the user actually saw);
    // a body claiming a different tool mints nothing.
    if (!toolName) return { status: 422 }
    if (toolName !== ask.toolName) return { status: 404 }
  }
  return {
    status: 200,
    outcome: verdictOutcome(action)!,
    grant: action === 'allow_session',
  }
}

/** The pending-holds listing for one lore session (GET /approvals). */
/** The BARE ask key: the card a CHILD ask renders on carries the
 * session-prefixed wire id (`<session>#<callId>` — map.ts's derivation, so the
 * card clears against the child's own result chip), while the parked ask is
 * keyed by dsh's raw call id. `#` never appears in a dsh call id, so stripping
 * the last segment is exact; a parent's card id passes through unchanged. */
export function bareAskKey(callId: string): string {
  const hash = callId.lastIndexOf('#')
  return hash >= 0 ? callId.slice(hash + 1) : callId
}

export function pendingHoldsFor(
  sessionId: string,
  state: { pending: Map<string, PendingAsk> } = { pending },
): { call_id: string; tool_name: string; message_id: string }[] {
  const out: { call_id: string; tool_name: string; message_id: string }[] = []
  for (const [callId, ask] of state.pending) {
    if (ask.loreSessionId === sessionId) {
      out.push({ call_id: callId, tool_name: ask.toolName, message_id: ask.messageId })
    }
  }
  return out
}

// ─── The asker (called by the Tool-API proxies / the report filing) ──────────

/** True when the session already granted this tool (the asker skips asking —
 * no audit pair, matching the deleted store's grant short-circuit). */
export function hasLoreGrant(loreSessionId: string, toolName: string): boolean {
  return grants.has(grantKey(loreSessionId, toolName))
}

/**
 * Ask dsh's approval service for one mutating call and await the user.
 *
 * The audit pair lands on the agent's session log (approval/asked relays as
 * the `lore/verdict-ask` mint — the card IS the question), the service
 * dispatches the `approval/request` waterfall, and THIS bridge's answerer
 * parks until the backend forwards a verdict (or the bound / the turn's abort
 * signal resolves it). Returns the closed outcome plus the reject words when
 * the user wrote any (the asker composes them into the call's own result).
 */
export async function askLoreApproval(
  exec: any, tctx: TurnToolCtx, toolName: string, reason: string,
): Promise<{ outcome: ApprovalOutcome; reason?: string }> {
  if (hasLoreGrant(tctx.loreSessionId, toolName)) {
    return { outcome: 'allowed-once' }
  }
  const approval = (bridgeCtx as any)?.approval
  const callId = String(exec?.callId ?? '')
  if (!approval || !callId) {
    // Fail closed with a diagnostic the model can act on.
    return { outcome: 'rejected', reason: 'approval service unavailable' }
  }
  const outcome: ApprovalOutcome = await approval.request({
    agent: exec.agent,
    toolName,
    callId,
    reason,
    signal: exec?.signal,
  })
  const words = rejectReasons.get(callId)
  rejectReasons.delete(callId)
  if (outcome === 'allowed-once') return { outcome }
  return { outcome, reason: words ?? '' }
}

// ─── The answerer + the driver HTTP endpoints ────────────────────────────────

/**
 * Register the answerer and remember the lookups. The listener passes through
 * (`next()`) on any ask it cannot own — no call id or no Lore turn context for
 * the asking agent's session — so a foreign asker keeps the fail-closed
 * default instead of hanging behind our bridge.
 */
export function registerLoreApprovalBridge(
  ctx: Context, ctxLookup: (dshId: string) => TurnToolCtx | undefined,
): void {
  bridgeCtx = ctx
  turnCtxFor = ctxLookup
  ctx.on('approval/request', (req: any, next: () => Promise<ApprovalOutcome>) => {
    const callId = String(req?.callId ?? '')
    const dshId = String(req?.agent?.session?.id ?? '')
    const tctx = callId && dshId ? turnCtxFor?.(dshId) : undefined
    if (!tctx) return next()
    // The park is SYNCHRONOUS: the entry exists before the waterfall can
    // return, so a verdict forwarded in the same tick as the ask still finds
    // it (the old store's subscribe-before-read ordering, in-process).
    return new Promise<ApprovalOutcome>((resolve) => {
      const entry: PendingAsk = {
        loreSessionId: tctx.loreSessionId,
        toolName: String(req.toolName ?? ''),
        messageId: tctx.messageId,
        resolve,
        expiresAt: Date.now() + LORE_APPROVAL_MAX_S * 1000,
        timer: setTimeout(() => {
          // Bounded ask: no verdict within the bound. Resolve rejected so the
          // agent sees a real answer, and leave the session-bound trace so the
          // owning session's LATE verdict is a 409, never a mute 404.
          pending.delete(callId)
          expired.set(callId, tctx.loreSessionId)
          if (expired.size > EXPIRED_TRACE_MAX) {
            const oldest = expired.keys().next().value
            if (oldest !== undefined) expired.delete(oldest)
          }
          rejectReasons.set(callId, 'hold_expired')
          resolve('rejected')
        }, LORE_APPROVAL_MAX_S * 1000),
      }
      pending.set(callId, entry)
    })
  })
}

/** Resolve one parked ask from the backend's verdict forward (POST /approvals).
 * Returns the HTTP status to answer with; 200 also resolved the ask. */
export function resolveVerdict(
  callId: string, sessionId: string, action: string,
  toolName: string | null, reason: string | null,
): number {
  const answer = postVerdictAnswer(callId, sessionId, action, toolName)
  if (answer.status !== 200) return answer.status
  const ask = pending.get(bareAskKey(callId))
  if (!ask) return 404
  if (answer.grant) grants.add(grantKey(sessionId, ask.toolName))
  if (action === 'reject') rejectReasons.set(bareAskKey(callId), reason ?? '')
  clearTimeout(ask.timer)
  pending.delete(bareAskKey(callId))
  ask.resolve(answer.outcome!)
  return 200
}

/** Resolve every parked ask of one lore session as rejected (the backend's
 * session-delete forward — POST /approvals/resolve), and forget everything
 * else the session left behind. Returns how many asks were resolved.
 *
 * The session is OVER: its grants can never be short-circuited against again,
 * and its expiry traces can never be probed again. Without this sweep neither
 * map is ever pruned — a long-lived driver accumulates one grant per approved
 * tool per session for its whole uptime. (`rejectReasons` drains itself: the
 * asker awaiting the resolve above reads and deletes its own entry.) */
export function resolveSessionAsks(sessionId: string, reason: string): number {
  let resolved = 0
  for (const [callId, ask] of [...pending]) {
    if (ask.loreSessionId !== sessionId) continue
    rejectReasons.set(callId, reason)
    clearTimeout(ask.timer)
    pending.delete(callId)
    ask.resolve('rejected')
    resolved += 1
  }
  for (const [callId, owner] of [...expired]) {
    if (owner === sessionId) expired.delete(callId)
  }
  const prefix = `${sessionId} `
  for (const key of [...grants]) {
    if (key.startsWith(prefix)) grants.delete(key)
  }
  return resolved
}

/** Test seam: wipe the bridge state between suites. */
export function resetLoreApprovalBridgeForTest(): void {
  for (const ask of pending.values()) clearTimeout(ask.timer)
  pending.clear()
  expired.clear()
  rejectReasons.clear()
  grants.clear()
  bridgeCtx = null
  turnCtxFor = null
}

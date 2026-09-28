/**
 * The Lore Tool-API proxies — the plugin's read/mutation surface.
 *
 * # SYSTEM: harness-driver (tools half). Lore's tools stay HTTP endpoints
 *   served by the Python backend; this module registers THIN proxies from the
 *   turn payload's `tools` (OpenAI fn schemas) onto the harness tool registry.
 *   Access checks live in Python and are never re-implemented here — the proxy
 *   knows tool names and shapes, never permissions (ecosystem-v3: the seam is
 *   deliberate and load-bearing).
 *
 * # ARCH: the mid-turn
 *   approval ask lives HERE. A confirm-mode mutating POST the backend refuses
 *   with 409 {code: confirmation_required} is retried after dsh's approval
 *   service asks the user (approvals.ts bridges the verdict); an approval
 *   re-POSTs with `apply: auto` + the X-Agent-Verdict marker, a rejection
 *   returns the user's words as the call's own result — the backend's own
 *   decision_store never parks a request again.
 *
 * # INVARIANT(security): the derived tool sets travel IN THE PAYLOAD —
 *   `mutating_tools`, `holdable_tools`, `region_tools` — and no set is ever
 *   re-declared here. Why: agent.tools is the single backend
 *   source for each driver decision; a second copy drifts silently, and a
 *   tool missing from a copy
 *   runs without an apply mode or outside the pinned region.
 *
 * # INVARIANT: MUTATING proxy tools NEVER declare `timeoutMs` — only the
 *   read-only ones do (step 9: dsh's tool-call timeout-policy arms the
 *   declared budget). Why the split: a mutating confirm call can ASK for the
 *   user's approval (approvals.ts), parking for up to LORE_APPROVAL_MAX_S
 *   (600 s) inside its own execute, and a declared budget would kill exactly
 *   that ask. A read-only proxy never asks, so its budget only bounds a HUNG
 *   call (a stuck backend) instead of burning the whole-turn deadline.
 *
 * # INVARIANT: the transport is node:http with setTimeout(0), never fetch.
 *   Why: undici's default 300 s headersTimeout aborts a slow call mid-flight;
 *   the wait ceiling is the backend's, not the client's.
 */

import http from 'node:http'
import type { Context } from '@deepseek-ai/cordis'

import { askLoreApproval } from './approvals.ts'

/** One Lore turn's tool-call identity + policy context, keyed by dsh session id. */
export interface TurnToolCtx {
  /** The Lore chat_session id — the verdict/hold join key (X-Agent-Session-Id). */
  loreSessionId: string
  /** The assistant message row this turn writes (X-Agent-Message-Id). */
  messageId: string
  /** The per-user delegated Tool-API credential from the turn payload. */
  agentKey: string
  /** 'auto' | 'confirm' — injected as `apply` on mutating calls. */
  applyMode: string
  /** The session's target document (the chat's pinned doc) — the report's
   * create_document parent_id. Null when the turn has no target. */
  document_id: string | null
  mutating: Set<string>
  holdable: Set<string>
  regionTools: Set<string>
  region: Record<string, unknown> | null
}

/** The STANDING half (plan agent-line-harness-lifecycle step 3): identity +
 * the payload-derived tool sets, registered ONCE per chat session — they key
 * the SESSION's permissions, not one request. `TurnToolCtx` above is the
 * merged view this and the request half compose into. */
export interface SessionToolCtx {
  loreSessionId: string
  agentKey: string
  mutating: Set<string>
  holdable: Set<string>
  regionTools: Set<string>
}

/** The PER-REQUEST half: what THIS turn writes and where — stamped by each
 * /followup (the driver-owned turn) and cleared at that turn's end. */
export interface TurnRequestCtx {
  messageId: string
  applyMode: string
  document_id: string | null
  region: Record<string, unknown> | null
}

const TOOL_API = process.env.LORE_TOOL_API_URL || 'http://backend:8001'

/** The shared secret this driver and the backend already use on the driver's
 * own endpoints — sent on an APPROVED retry so the backend can tell our
 * verdict marker from any agent key's claim (see callToolApi). */
const DRIVER_SECRET = process.env.LORE_DRIVER_SECRET || ''

/** The machine-readable refusal code for a call that must be approved before
 * it applies (the backend's confirm cell — see _refuse_unconfirmable there). */
const CONFIRMATION_REQUIRED = 'confirmation_required'

/** The per-call hang bound READ-ONLY proxies declare (dsh's timeout-policy
 * arms it): a stuck backend call fails in 120 s instead of burning the
 * whole-turn wall-clock deadline. Mutating proxies declare NOTHING — an
 * approval ask legitimately parks for up to LORE_APPROVAL_MAX_S. */
export const READ_TOOL_TIMEOUT_MS = 120_000

/** The registry, split (plan agent-line-harness-lifecycle step 3): the
 * standing half per dsh session id, the per-request half per TURN. The
 * merged lookup answers only when BOTH halves exist — a tool call with no
 * turn in flight still errors (the plan keeps that refusal deliberately),
 * and between driver-owned turns only the standing half remains. */
const sessionCtx = new Map<string, SessionToolCtx>()
const requestCtx = new Map<string, TurnRequestCtx>()

/** The standing half: once per chat session (upsert — a later payload
 * refreshes identity/sets). Cleared on session delete. */
export function setSessionToolCtx(dshId: string, ctx: SessionToolCtx): void {
  sessionCtx.set(dshId, ctx)
}

export function clearSessionToolCtx(dshId: string): void {
  sessionCtx.delete(dshId)
}

/** The per-request half: stamped at each turn's start, cleared at its end. */
export function stampTurnRequestCtx(dshId: string, ctx: TurnRequestCtx): void {
  requestCtx.set(dshId, ctx)
}

export function clearTurnRequestCtx(dshId: string): void {
  requestCtx.delete(dshId)
}

/** The merged turn ctx — undefined unless BOTH halves exist (see the
 * registry comment). The approval bridge's registration input. */
export function loreTurnCtxFor(dshId: string): TurnToolCtx | undefined {
  const standing = sessionCtx.get(dshId)
  const request = requestCtx.get(dshId)
  if (!standing || !request) return undefined
  return {
    loreSessionId: standing.loreSessionId,
    messageId: request.messageId,
    agentKey: standing.agentKey,
    applyMode: request.applyMode,
    document_id: request.document_id,
    mutating: standing.mutating,
    holdable: standing.holdable,
    regionTools: standing.regionTools,
    region: request.region,
  }
}

/** Registered proxy disposers by tool name (the process-lifetime union). */
const registered = new Map<string, () => void>()

/**
 * Register proxy tools for every payload schema not already registered.
 *
 * `includeMutating=false` is the READ-ONLY line (kept for bisection and
 * rollback): mutating tools stay unregistered, so the model cannot call what
 * does not exist. Production passes `true` — mutating proxies honour
 * the payload's mutating/holdable/region sets exactly.
 */
export function registerLoreTools(
  ctx: Context, tools: unknown[], includeMutating: boolean,
  mutating: Set<string>,
): void {
  for (const entry of Array.isArray(tools) ? tools : []) {
    const fn = (entry as any)?.function
    const name = typeof fn?.name === 'string' ? fn.name : ''
    if (!name || registered.has(name)) continue
    if (!includeMutating && mutating.has(name)) continue
    const dispose = ctx.tools.register({
      name,
      description: String(fn?.description ?? ''),
      parameters: (fn?.parameters ?? { type: 'object', properties: {} }) as Record<string, unknown>,
      output: {
        schema: { type: 'string' },
        render: (_args: unknown, value: unknown) => [
          { type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value) },
        ],
      },
      // No timeoutMs on MUTATING proxies — see the module INVARIANT (an
      // approval ask must outlive 600 s). Read-only proxies declare the hang
      // bound timeout-policy arms.
      ...(mutating.has(name) ? {} : { timeoutMs: READ_TOOL_TIMEOUT_MS }),
      async execute(args: unknown, exec: any): Promise<string> {
        return await proxyExecute(name, args as Record<string, unknown>, exec)
      },
    })
    registered.set(name, dispose)
  }
}

export function disposeLoreTools(): void {
  for (const dispose of registered.values()) dispose()
  registered.clear()
}

/** The registered proxy names (the process-lifetime union) — the SERVED set
 * the skills filter clips its deny-list against (`tools.restrict` fails loud
 * on unknown names, so config drift must be pre-filtered, never crashed). */
export function registeredLoreToolNames(): Set<string> {
  return new Set(registered.keys())
}

/** POST one Tool-API call, forwarding the identity headers the hold keys on. */
async function proxyExecute(
  tool: string, args: Record<string, unknown>, exec: any,
): Promise<string> {
  const dshId = String(exec?.agent?.session?.id ?? '')
  const tctx = loreTurnCtxFor(dshId)
  if (!tctx) {
    // A call outside a Lore-driven turn (nothing bridges the two session
    // identities). Explicit error, never a headerless anonymous call.
    const err = { error: `${tool}: no Lore turn context for this session`, status_code: 0 }
    return JSON.stringify(err)
  }
  const isMutating = tctx.mutating.has(tool)
  // Guard: a mutating tool ALWAYS needs arguments; empty means the
  // model's JSON was unparsable — answer an actionable error, never POST {}.
  if (isMutating && (!args || Object.keys(args).length === 0)) {
    return JSON.stringify({
      error: `malformed tool arguments: ${tool} received no parsable arguments — ` +
        're-emit the call with valid JSON',
    })
  }
  const isRegionCarrying = tctx.regionTools.has(tool)
  const body = isMutating
    ? { ...args, apply: tctx.applyMode,
        ...(tctx.region && isRegionCarrying ? { region: tctx.region } : {}) }
    : args
  return await callToolApiWithApproval(tctx, tool, body, exec, dshId)
}

/**
 * The Tool-API hop WITH the approval retry: a 409 {code: confirmation_required}
 * is the backend asking the user first (the confirm cell — the single
 * authority for apply-mode resolution). The ask parks inside dsh's approval
 * service (approvals.ts bridges the verdict); allowed re-POSTs as
 * apply:auto + the X-Agent-Verdict marker, rejected returns the user's words
 * as the call's own result so the model replans inside the same turn.
 */
export async function callToolApiWithApproval(
  tctx: TurnToolCtx, tool: string, body: Record<string, unknown>,
  exec: any, dshId: string,
): Promise<string> {
  const raw = await callToolApi(tctx, tool, body, exec, dshId)
  const envelope = parseEnvelope(raw)
  if (envelope?.status_code !== 409
      || envelope.code !== CONFIRMATION_REQUIRED) {
    return raw
  }
  const ask = await askLoreApproval(
    exec, tctx, tool,
    `Lore mutating call ${tool} needs the user's approval before it applies`,
  )
  if (ask.outcome !== 'allowed-once') {
    return JSON.stringify({ status: 'rejected', reason: ask.reason || '' })
  }
  return await callToolApi(
    tctx, tool, { ...body, apply: 'auto' }, exec, dshId, true,
  )
}

/** Parse a result envelope (the proxy's own JSON shapes) without throwing. */
function parseEnvelope(raw: string): Record<string, any> | null {
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : null
  } catch {
    return null
  }
}

/** The HTTP hop itself (node:http, no timeout — the backend bounds the wait).
 * `approved` marks a call the user already allowed through dsh's approval
 * service: it applies as auto with the X-Agent-Verdict marker (the backend's
 * replacement for its own deleted verdict_approved flag).
 *
 * # INVARIANT(security): the marker rides WITH the driver secret. Why: the
 * backend honors it only on a driver-attested request — the Tool-API is
 * reachable by any agent-capable key, and an unattested marker would let its
 * holder self-approve every write the confirm cell exists to stop. */
export async function callToolApi(
  tctx: TurnToolCtx, tool: string, body: Record<string, unknown>,
  exec: any, dshId: string, approved = false,
): Promise<string> {
  const started = Date.now()
  const payload = JSON.stringify(body)
  const url = new URL(`${TOOL_API}/api/tool/${tool}`)
  return await new Promise<string>((resolve, reject) => {
    const req = http.request({
      hostname: url.hostname,
      port: url.port,
      path: url.pathname,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(payload),
        Authorization: `Bearer ${tctx.agentKey}`,
        // The identity bridge: the approval keys on the CALL id, resolves through
        // the SESSION row, and re-renders under the MESSAGE id. callId is the
        // harness's own model-requested call id (exec.callId) — unique per
        // call, so no plugin-side minting can collide or drift.
        'X-Agent-Call-Id': String(exec?.callId ?? ''),
        'X-Agent-Session-Id': tctx.loreSessionId,
        'X-Agent-Message-Id': tctx.messageId,
        ...(approved
          ? { 'X-Agent-Verdict': 'allowed-once', 'X-Driver-Secret': DRIVER_SECRET }
          : {}),
      },
    }, res => {
      let buf = ''
      res.setEncoding('utf8')
      res.on('data', (c: string) => { buf += c })
      res.on('end', () => {
        const ms = Date.now() - started
        console.error(
          `[lore-tools] ${tool} session=${dshId} call=${exec?.callId ?? ''} ` +
          `status=${res.statusCode} ms=${ms}`)
        if ((res.statusCode ?? 0) >= 400) {
          // Error envelope: a non-2xx is a FAILURE and must reach the
          // model AND the backend chip classifier as one ({error,status_code}
          // are the keys _classify_outcome reads). The machine-readable `code`
          // rides along when the backend's refusal carried one (the approval
          // retry keys on confirmation_required).
          const envelope: Record<string, unknown> = {
            error: `${tool} failed ${res.statusCode}: ${buf.slice(0, 300)}`,
            status_code: res.statusCode,
          }
          const code = envelopeCode(buf)
          if (code) envelope.code = code
          resolve(JSON.stringify(envelope))
          return
        }
        resolve(buf)
      })
    })
    // No timeout: an approval ask can park here for up to LORE_APPROVAL_MAX_S.
    req.setTimeout(0)
    // The turn's abort signal must tear the socket down AND keep the abort
    // THROWING (the loop's emergency-stop keys off the rejection).
    exec?.signal?.addEventListener('abort', () => req.destroy(new Error('aborted')))
    req.on('error', reject)
    req.write(payload)
    req.end()
  })
}

/** The refusal code out of a FastAPI error body — the confirm cell raises
 * HTTPException(detail={"code": ..., "detail": ...}), so the machine-readable
 * code sits NESTED under `detail` (a plain-string detail carries none). */
function envelopeCode(buf: string): string | null {
  try {
    const parsed = JSON.parse(buf)
    if (parsed && typeof parsed === 'object') {
      if (typeof parsed.code === 'string') return parsed.code
      const nested = parsed.detail
      if (nested && typeof nested === 'object' && typeof nested.code === 'string') {
        return nested.code
      }
    }
  } catch {
    // A plain-string detail — not machine-readable.
  }
  return null
}


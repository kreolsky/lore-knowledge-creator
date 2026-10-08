/**
 * The driver HTTP endpoints — the shared secret gate (authorized), the
 * body read, the router (handleRequest), health, /capability and the
 * approval verdict endpoints.
 */

import http from 'node:http'
import type { Context } from '@deepseek-ai/cordis'

import { gatewayOf, resolveModelCaps } from './caps.ts'
import {
  pendingHoldsFor, resolveSessionAsks, resolveVerdict,
} from './approvals.ts'
import { clearSessionToolCtx } from './tools.ts'
import { sessionEntries, sessionLeaf, type SessionMap } from './sessions.ts'
import { followup, stop } from './turn.ts'
import type { EventsChannel, SessionEventTap } from './ws-events.ts'
import type { SessionStreamBaselines } from './stream-baselines.ts'

export const PORT = Number(process.env.PORT || 8090)
const SECRET = process.env.LORE_DRIVER_SECRET || ''

export function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let raw = ''
    req.setEncoding('utf8')
    req.on('data', (c: string) => { raw += c })
    req.on('end', () => resolve(raw))
    req.on('error', reject)
  })
}

// ── The driver endpoints. apply() keeps only the server lifecycle; each
// endpoint is its own handler over the shared dsh session runner
// (resumeOrCreate / seedForkSession below), and the turn's own phases —
// activation restore, the restriction lifecycle, the session listener, the
// fault paths — are each their own unit. ──────────────────────────────────────

export async function handleRequest(
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

export function authorized(req: http.IncomingMessage): boolean {
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

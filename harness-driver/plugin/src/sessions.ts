/**
 * Session identity + the dsh session log — the SessionMap (lore id → dsh
 * id, read-only legacy resolution), the fork seeding under the caller's own
 * id, the one balanced read path, resume-or-create and the served-toolset
 * activation restore.
 */

import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import type { Context } from '@deepseek-ai/cordis'
import { buildForkSeed, interruptedTurnClosers, SessionId, SessionSeq } from '@deepseek-ai/dsh-session'

import { projectSessionEntries } from './entries.ts'
import { PROVIDER } from './ensure.ts'
import { restoreActivatedSkills, type ResolvedSkill } from './skills.ts'
import { authorized, readBody } from './http.ts'
import { followupTurns } from './turn.ts'
import type { SessionStreamBaselines } from './stream-baselines.ts'

/** lore session id → current dsh session id (persisted beside the session
 * logs, under the mounted sessions root — survives container replacement). */
export class SessionMap {
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

/** The pair a fork seeds its session with: the route the parent last
 * requested at or before the branch point (its `request/header` — the same
 * fact dsh's retry reader and titler read; provider and model ride the same
 * header config), else the fresh-install pair `{provider: 'lore', model: ''}`
 * — no fallback model. The next turn re-selects its own model and route
 * either way. */
export function forkModel(
  events: readonly unknown[], seq: number,
): { provider: string; model: string } {
  for (let i = Math.min(seq, events.length - 1); i >= 0; i -= 1) {
    const ev = events[i] as {
      type?: string
      data?: { header?: { config?: { provider?: unknown; model?: unknown } } }
    }
    if (ev?.type !== 'request/header') continue
    const config = ev.data?.header?.config
    const model = config?.model
    if (typeof model === 'string' && model) {
      const provider = config?.provider
      return {
        provider: typeof provider === 'string' && provider ? provider : PROVIDER,
        model,
      }
    }
  }
  return { provider: PROVIDER, model: '' }
}

/** Shared dsh session runner, fork create half: build dsh's OWN fork seed for
 * the boundary and flush a NEW session seeded with it (the create→flush→dispose
 * lifecycle a fork ride shares with the turn runner's create path). The id is
 * the CALLER's choice — /session-fork passes the branch's Lore session id and
 * touches neither the map nor any subscription (dsh id = Lore id). `parent` is
 * the session the seed was read from. */
async function seedForkSessionCreate(
  ctx: Context, freshId: string, parent: string,
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
    agentOptions: forkModel(events, seq),
    setup: (agentCtx: Context) => {
      agentCtx.systemPrompt.section({ name: 'lore-turn', order: 0, text: '', complete: true })
    },
  })
  try {
    await ctx.sessions.flush(handle.agent.session)
  } finally {
    await handle.dispose()
  }
}

/** The one read path over a dsh session log: open a read handle, take every
 * committed event, close. A missing session rejects with `session "<id>" not
 * found` — callers that tolerate that miss catch it themselves.
 *
 * A NON-live session's read is BALANCED in memory with dsh's crash closers:
 * `interruptedTurnClosers(events)` (the same deterministic closers dsh's own
 * cold read — session-query readColdSessionLog — and its resume repair
 * append), so a log whose writer died mid-turn replays as a turn that ENDED
 * (turn/end reason 'interrupted', the halt mint at its seq) instead of a turn
 * open forever. Nothing is written back: dsh's next resume persists the same
 * closers itself (same function, same seqs). Without the closers a driver
 * crash would stay open until the backend's no-progress deadline.
 *
 * "Live" is the REGISTERED driver-owned turn — the session's dsh id is a key
 * of `followupTurns` — never "an agent might be running": a read inside the
 * register window (after prepareTurnDrive, before the key lands) gets
 * in-memory closers for a log resume is about to repair with the same ones,
 * which is harmless. A live session's read stays verbatim: closers on a
 * running turn would close it in every replay while it streams. */
async function readSessionEvents(ctx: Context, dshId: string): Promise<unknown[]> {
  const persistence = ctx.get('sessionPersistence')
  if (!persistence) throw new Error('session persistence is not configured')
  const handle = await persistence.open(SessionId(dshId as never), 'read')
  try {
    const { events } = await handle.read(0, undefined)
    return followupTurns.has(dshId)
      ? (events as unknown[])
      : [...(events as unknown[]), ...interruptedTurnClosers(events as never)]
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
export async function sessionEntries(
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
  const dshId = map.get(loreId) as string
  let events: unknown[] = []
  try {
    events = await readSessionEvents(ctx, dshId)
  } catch (err) {
    // The one tolerated miss: a session that was never created. Anything else
    // is a READ FAILURE and must surface — the backend renders an explicit
    // error, never an empty thread (DriverTimelineUnavailable's INVARIANT).
    if (!(err instanceof Error && /not found/.test(err.message))) throw err
  }
  // The transport terminal rides the replay for the SAME session class the
  // crash closers balance — NON-live (`followupTurns`, the registered
  // driver-owned turn). A live session's running turn is open (no terminal)
  // and its previous turns' terminals stay the live push's to deliver: a
  // replayed one would be the newer turn's registration to close.
  const terminals = !followupTurns.has(dshId)
  // The open turn's streamed-text baseline: the live-stream fold of the
  // session's CURRENT dsh id (stream-baselines.ts) — attached to the open
  // turn only, and only while an attempt is active (entries.ts's gate).
  const turns = projectSessionEntries(
    events as any[], sinceSeq, baselines.snapshot(dshId), terminals)
  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify(turns))
}

/** Does a dsh session log exist under this id? An open of a never-created
 * session rejects the same way resumeOrCreate's resume does (`not found`);
 * anything else is a real read failure and re-throws. */
async function sessionExists(ctx: Context, dshId: string): Promise<boolean> {
  const persistence = ctx.get('sessionPersistence')
  if (!persistence) throw new Error('session persistence is not configured')
  try {
    const handle = await persistence.open(SessionId(dshId as never), 'read')
    await handle.close()
    return true
  } catch (err) {
    if (err instanceof Error && /not found/.test(err.message)) return false
    throw err
  }
}

// ── POST /session-fork — the session-per-branch fork primitive.
// A branch is its OWN dsh session whose id EQUALS the
// Lore session id: seed a NEW session under the CALLER-CHOSEN new_lore_id
// through dsh's buildForkSeed (the create half seedForkSessionCreate) and
// touch NO map entry and NO subscription — nothing
// is re-pointed, sessions only ever append. `source_dsh_id` resolves through
// SessionMap.get (identity default): a legacy chat's bare lore id maps to
// its current dsh id. IDEMPOTENT: a dsh session new_lore_id that already
// exists answers 200 without re-seeding — a crash between the fork and the
// backend clearing the row's seed_source_* makes the next turn call this
// again, and re-seeding would fork a second log under the same id.
export async function sessionFork(
  ctx: Context, map: SessionMap,
  req: http.IncomingMessage, res: http.ServerResponse,
): Promise<void> {
  if (!authorized(req)) {
    res.writeHead(401).end('unauthorized')
    return
  }
  const body = JSON.parse(await readBody(req)) as {
    source_dsh_id?: unknown; seq?: unknown; new_lore_id?: unknown
  }
  const sourceRaw = typeof body.source_dsh_id === 'string' ? body.source_dsh_id : ''
  const newLoreId = typeof body.new_lore_id === 'string' ? body.new_lore_id : ''
  if (!sourceRaw || !newLoreId || body.seq === undefined) {
    res.writeHead(422).end('source_dsh_id, seq and new_lore_id are required')
    return
  }
  const seq = Number(body.seq)
  // Non-negative: a negative could never address a real row, and refusing is
  // better than a coerced no-op. No null here — the primitive has no
  // root-fork mode (a branch prefix with no completed turn is never seeded;
  // its log starts fresh on the first turn).
  if (!Number.isInteger(seq) || seq < 0) {
    res.writeHead(422).end('seq must be a non-negative integer')
    return
  }
  // Idempotency first: an existing branch log answers 200 without reading
  // the source or seeding — the crash-retry caller must not fork twice.
  if (await sessionExists(ctx, newLoreId)) {
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: true, existed: true }))
    return
  }
  const source = map.get(sourceRaw)
  let events: unknown[]
  try {
    events = await readSessionEvents(ctx, source)
  } catch (err) {
    // A missing source log is an unresolvable branch point, not a server
    // failure.
    if (!(err instanceof Error && /not found/.test(err.message))) throw err
    console.error(`[lore-driver] fork UNRESOLVABLE source=${source} missing`)
    res.writeHead(422).end('branch point not resolvable in session')
    return
  }
  // The boundary guard (the v4 contiguity contract —
  // seq-addressed rows): a forkable branch point is a row whose position IS
  // its seq and whose kind is `turn/end`. Anything else is unresolvable, and
  // a fork must never seed to a boundary it cannot prove.
  const boundary = events[seq] as { seq?: number; type?: string } | undefined
  if (!boundary || boundary.seq !== seq || boundary.type !== 'turn/end') {
    console.error(`[lore-driver] fork UNRESOLVABLE new=${newLoreId} source=${source} ` +
      `seq=${seq} events=${events.length}`)
    res.writeHead(422).end('branch point not resolvable in session')
    return
  }
  if (process.env.LORE_DRIVER_DEBUG_EVENTS) {
    console.error(`[lore-driver] session-fork ${source} -> ${newLoreId} ` +
      `seq=${seq} events=${events.length}`)
  }
  await seedForkSessionCreate(ctx, newLoreId, source, events, seq)
  res.writeHead(200, { 'Content-Type': 'application/json' })
  res.end(JSON.stringify({ ok: true }))
}

/** Shared dsh session runner, turn half: resume the persisted session or
 * create it on the first turn (resume-or-create from the plugin header). */
export async function resumeOrCreate(
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
export async function restoreActivation(
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

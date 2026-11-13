/**
 * Personal MCP servers — the per-turn mount of the user's own MCP servers
 * into the session agent's scope (the dsh mcp-client plugin, one instance
 * per server, mounted on the AGENT's context so two users never share a
 * transport or a namespace).
 *
 * # ARCH: the mount is CLAIM-time and fingerprint-diffed. The prep's
 *   mcp_servers entries (name/url/headers/secret_values, backend-rendered
 *   per user) mount at the claim without awaiting — the claim never stalls;
 *   an unchanged name+url+headers fingerprint is left alone so an unchanged
 *   server never reconnects mid-conversation (prefix-cache stability), a
 *   changed key remounts on the next turn, and a dropped server is disposed
 *   before the turn starts. Idle dispose drops connections and in-memory
 *   keys with the agent; a fresh agent remounts everything.
 *
 * # ARCH: the bounded wait lives in the agent-scoped `system-prompt/assemble`
 *   waterfall, NOT in `agent/pre-step`: preStep assembles the prompt (and
 *   snapshots tool schemas, agent-loop/src/agent.ts preStep → assemble)
 *   BEFORE the `agent/pre-step` event fires, so a wait there cannot reach the
 *   claiming request. The assemble listener awaits the pending mounts raced
 *   against MCP_MOUNT_WAIT_MS, then re-projects the agent's tool schemas
 *   from the live registry — dsh has no connect timeout (SDK default 60 s),
 *   so a dead server costs at most the bound and its tools are simply
 *   absent. When no server is mounted the listener is a bare `next()` —
 *   zero overhead, no tool rewrite.
 *
 * # ARCH: an external tool is NAMED external in its own description. dsh
 *   copies a server's tool description verbatim (mcp-client tools.ts), so a
 *   server serving tools named like the host's own (another Lore's gateway:
 *   search_materials, read_document, …) reaches the model as byte-identical
 *   twins differing only in the mcp__<server>__ prefix — and the model calls
 *   the twin for work on THIS project. While any server is mounted, every
 *   assembly runs `describeMcpTools`: each mcp__<name>__ tool gets an
 *   external-service prefix, and a twin (raw name == a native tool in the
 *   same assembly) a sentence naming the native tool to use instead. Nothing
 *   is hidden — a server connected to work with another Lore keeps working.
 *   It lives in the SAME assemble listener as the re-projection: a second
 *   listener would be overwritten by the re-projection or overwrite it,
 *   depending on order. The text is deterministic per (server, tool set), so
 *   the request prefix cache stays stable across turns.
 *
 * # INVARIANT(security): rendered headers and secret_values live in DRIVER
 *   MEMORY only — never a log line, never a frame, never the model context.
 *   The post-execute scrub replaces each secret's raw, base64 and
 *   url-encoded forms with `[secret]` in every mcp__ tool result — success
 *   values (the accept `value` arm) and error content (the accept `content`
 *   arm; the registry refuses a value replacement on a failed result) —
 *   before the result reaches the session log and the model. A server
 *   echoing `Authorization` back must never put the token on the wire.
 *
 * # INVARIANT(security): every mcp__ call that is not marked read-only ASKS
 *   through the existing verdict bridge before it runs (tools/pre-execute →
 *   {kind:'ask'}): text from a remote page can instruct the model to call a
 *   write tool under the user's own rights. The server's own
 *   annotations.readOnlyHint is accepted (operator ruling: the server already
 *   holds the user's key, a lying mark adds nothing to what it can do); dsh's
 *   sync drops annotations, so the mount also lists them through one
 *   short-lived SDK client with the same headers. A server whose definer set
 *   `auto_approve` runs every tool unasked (operator ruling: internal
 *   read-only servers that ship no marks; the admin or moderator who defines
 *   the server owns that call for all its users). An unmarked or
 *   unlisted tool asks once per chat — "allow for session" grants through
 *   the same bridge the Tool-API proxies use.
 */

import { createHash } from 'node:crypto'
import type { Context } from '@deepseek-ai/cordis'
import { Client, StreamableHTTPClientTransport } from '@modelcontextprotocol/client'
import * as mcpClient from '@deepseek-ai/dsh-mcp-client'
// publicToolName is the naming contract (a lossy normalization appends a
// 12-hex hash) — the marks must key by exactly the names dsh registers.
import { publicToolName } from '@deepseek-ai/dsh-mcp-client/src/tools.ts'

import { hasLoreGrant, revokeServerGrants } from './approvals.ts'

/** The bound on ONE mount's contribution to the turn's first assembly. */
export const MCP_MOUNT_WAIT_MS = 5000

/** The per-call timeout of the short-lived read-only-marks listing. */
const MARKS_TIMEOUT_MS = 5000

/** dsh's own serverName contract (SERVER_NAME_PATTERN). */
const SERVER_NAME_RE = /^[A-Za-z0-9_-]{1,32}$/

/** One MCP server as the backend rendered it into the turn payload. The
 * headers carry the user's secrets already rendered — they are never logged. */
export interface McpServerWire {
  name: string
  url: string
  /** The model-facing title (whitespace-collapsed; empty ⇒ the name). */
  title: string
  headers: Record<string, string>
  /** The raw secret values the scrub replaces in tool results. */
  secret_values: string[]
  /** The definer's ruling: every tool of this server runs with no ask. */
  auto_approve: boolean
}

/** The payload's `mcp_servers` array → validated entries; an invalid entry is
 * dropped with a loud log naming only its NAME (never headers or url). */
export function parseMcpServers(body: Record<string, any>): McpServerWire[] {
  const raw = Array.isArray(body.mcp_servers) ? body.mcp_servers : []
  const out: McpServerWire[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const name = typeof item.name === 'string' ? item.name : ''
    const url = typeof item.url === 'string' ? item.url : ''
    const headersIn = item.headers
    const headers: Record<string, string> = {}
    let headersOk = headersIn && typeof headersIn === 'object' && !Array.isArray(headersIn)
    if (headersOk) {
      for (const [key, value] of Object.entries(headersIn as Record<string, unknown>)) {
        if (typeof value !== 'string') { headersOk = false; break }
        headers[key] = value
      }
    }
    if (!SERVER_NAME_RE.test(name) || !/^https?:\/\//i.test(url) || !headersOk) {
      console.error(`[lore-mcp] dropped invalid mcp_servers entry name=${name || '(missing)'}`)
      continue
    }
    const secretValues = (Array.isArray(item.secret_values) ? item.secret_values : [])
      .filter((v: unknown): v is string => typeof v === 'string' && v !== '')
    const title = (typeof item.title === 'string' ? item.title : '').split(/\s+/).filter(Boolean).join(' ') || name
    // Anything but a literal true asks — the fail-safe default.
    const autoApprove = item.auto_approve === true
    out.push({ name, url, title, headers, secret_values: secretValues, auto_approve: autoApprove })
  }
  if (out.length > 0) {
    console.error(`[lore-mcp] prep ${out.length} server(s): ${out.map((s) => s.name).join(',')}`)
  }
  return out
}

/** The change detector: name + url + a hash of the rendered headers. A key
 * rotation changes the hash; an unchanged server never remounts. */
export function mcpFingerprint(server: McpServerWire): string {
  const headers = createHash('sha256')
    .update(JSON.stringify(Object.entries(server.headers).sort(([a], [b]) => a.localeCompare(b))))
    .digest('hex')
  return `${server.name}\0${server.url}\0${headers}`
}

/** One live mount: the plugin fiber plus the settled read-only marks. */
export interface MountedMcp {
  fingerprint: string
  fiber: { dispose(): Promise<void>; await(): Promise<unknown> }
  /** The marks listing, awaited by the turn's bounded wait and re-settled
   * here (null until it settles; a failure settles empty). */
  marks: Promise<Set<string>>
  marksSet: Set<string> | null
  /** public → raw tool name from the same listing (empty until it settles or
   * when it failed). How a twin is found without parsing a public name. */
  rawByPublic: Map<string, string>
}

/** What the seam reads off the session agent (structural — SessionAgent
 * satisfies it; mcp-servers.ts never imports session-agents.ts). */
export interface McpSeamSite {
  agentCtx: Context | null
  current: {
    mcpServers: McpServerWire[]
    standingCtx: { loreSessionId: string }
  } | null
  mcpMounted: Map<string, MountedMcp>
  /** Mounts not yet reflected in an assembly; consumed by the assemble wait. */
  mcpPending: Promise<unknown>[]
}

/** Diff the current prep's servers against what is mounted: dispose what
 * dropped or changed, mount what is new — all without awaiting (the claim
 * never stalls; the assembly's bounded wait is what makes the tools arrive). */
export function syncMcpServers(entry: McpSeamSite): void {
  const agentCtx = entry.agentCtx
  if (!agentCtx) return
  const wanted = entry.current?.mcpServers ?? []
  const wantByName = new Map(wanted.map((server) => [server.name, server]))
  for (const [name, mounted] of entry.mcpMounted) {
    const next = wantByName.get(name)
    if (next && mcpFingerprint(next) === mounted.fingerprint) continue
    entry.mcpMounted.delete(name)
    // Fire-and-forget: disposal unregisters the server's tools; a failure is
    // logged, never thrown into the claim.
    void mounted.fiber.dispose().catch((err) => {
      console.error(`[lore-mcp] dispose failed for ${name}: ${String(err)}`)
    })
    // A changed or dropped mount must not keep "allow for session" grants:
    // the remount under the same name is a DIFFERENT endpoint, and a grant
    // minted against the old one must not cover it.
    const sessionId = entry.current?.standingCtx.loreSessionId
    if (sessionId) revokeServerGrants(sessionId, name)
  }
  if (wanted.length === 0) return
  for (const server of wanted) {
    if (entry.mcpMounted.has(server.name)) continue
    const mounted = mountServer(agentCtx, server)
    if (!mounted) continue
    entry.mcpMounted.set(server.name, mounted)
    // The turn's bounded wait covers the fiber (connect + tool sync) AND the
    // marks listing: a call that reaches pre-execute before its marks settle
    // would ask even for a read-only tool — with a real model the listing
    // wins by seconds, but the wait makes it deterministic. Errors are
    // contained so the race never rejects.
    entry.mcpPending.push(
      mounted.fiber.await().catch((err) => {
        console.error(`[lore-mcp] mount failed for ${server.name}: ${String(err)}`)
      }),
      mounted.marks,
    )
  }
}

/** Mount one server (plus its marks listing) onto the agent scope. Null when
 * the plugin load itself refused — logged, and the server simply stays off. */
function mountServer(agentCtx: Context, server: McpServerWire): MountedMcp | null {
  let fiber: MountedMcp['fiber']
  try {
    fiber = agentCtx.plugin(mcpClient, {
      transport: 'streamable-http',
      serverName: server.name,
      url: server.url,
      headers: server.headers,
      failOnStartupError: false,
    })
  } catch (err) {
    console.error(`[lore-mcp] load refused for ${server.name}: ${String(err)}`)
    return null
  }
  const mounted: MountedMcp = {
    fingerprint: mcpFingerprint(server), fiber, marksSet: null,
    marks: Promise.resolve(new Set()), rawByPublic: new Map(),
  }
  mounted.marks = listServerTools(server)
    .then(({ marks, rawByPublic }) => {
      mounted.marksSet = marks
      mounted.rawByPublic = rawByPublic
      return marks
    })
    .catch((err) => {
      // An unlisted server asks per tool — the fail-safe default.
      console.error(`[lore-mcp] marks listing failed for ${server.name}: ${String(err)}`)
      return new Set<string>()
    })
  return mounted
}

/** Dispose every mount of an agent (idle dispose / teardown): connections
 * and in-memory keys drop with the agent; a fresh agent remounts all. */
export async function disposeMcpServers(entry: McpSeamSite): Promise<void> {
  entry.mcpPending.length = 0
  const mounted = [...entry.mcpMounted.values()]
  entry.mcpMounted.clear()
  await Promise.allSettled(mounted.map((m) => m.fiber.dispose()))
}

/** One short-lived listing with the SAME headers the mount uses, keeping the
 * public names of every tool the server itself marks read-only (dsh's sync
 * drops annotations, so this is the only place the marks exist) and the
 * public → raw name map the twin detection reads. */
async function listServerTools(server: McpServerWire): Promise<{ marks: Set<string>; rawByPublic: Map<string, string> }> {
  const client = new Client({ name: 'lore-driver-marks', version: '0.0.1' }, { capabilities: {} })
  const transport = new StreamableHTTPClientTransport(
    new URL(server.url), { requestInit: { headers: server.headers } })
  try {
    await client.connect(transport)
    const response = await client.listTools(undefined, { timeout: MARKS_TIMEOUT_MS })
    const marks = new Set<string>()
    const rawByPublic = new Map<string, string>()
    for (const tool of response.tools) {
      const publicName = publicToolName(server.name, tool.name)
      rawByPublic.set(publicName, tool.name)
      const annotations = tool.annotations as { readOnlyHint?: unknown } | undefined
      if (annotations?.readOnlyHint === true) marks.add(publicName)
    }
    return { marks, rawByPublic }
  } finally {
    await client.close().catch(() => {})
  }
}

/** One mounted server as the description rewrite reads it. */
export interface McpDescribedServer {
  name: string
  title: string
  rawByPublic: ReadonlyMap<string, string>
}

/** The server owning an `mcp__<name>__<tool>` name, by the LONGEST matching
 * prefix: SERVER_NAME_RE admits `__`, so `mcp__x__y__t` of a server `x__y`
 * also starts with server `x`'s prefix. Exact because SERVER_NAME_RE admits
 * only characters dsh does not normalize. */
export function owningServer<S extends { name: string }>(toolName: string, servers: readonly S[]): S | undefined {
  return servers
    .filter((s) => toolName.startsWith(`mcp__${s.name}__`))
    .reduce<S | undefined>((best, s) => (!best || s.name.length > best.name.length ? s : best), undefined)
}

/** Prefix every mounted server's tool description with its external origin,
 * and name the native tool a twin shadows (see module ARCH). Pure; native
 * tools pass through untouched. Attribution: `owningServer`. */
export function describeMcpTools<T extends { name: string; description: string }>(
  tools: readonly T[], servers: readonly McpDescribedServer[],
): T[] {
  if (servers.length === 0) return [...tools]
  const native = new Set(tools.filter((t) => !t.name.startsWith('mcp__')).map((t) => t.name))
  return tools.map((tool) => {
    if (!tool.name.startsWith('mcp__')) return tool
    const server = owningServer(tool.name, servers)
    if (!server) return tool
    let prefix = `[External MCP service "${server.title}" (server ${server.name}) — acts on that service, not on this project.] `
    const raw = server.rawByPublic.get(tool.name)
    if (raw !== undefined && native.has(raw)) {
      prefix += `Twin of this project's own \`${raw}\`: the same operation, run on the external service. For this project call \`${raw}\`. `
    }
    return { ...tool, description: prefix + tool.description }
  })
}

/** The mounted servers joined with the current prep's titles. */
function describedServers(entry: McpSeamSite): McpDescribedServer[] {
  const titles = new Map((entry.current?.mcpServers ?? []).map((s) => [s.name, s.title]))
  return [...entry.mcpMounted].map(([name, mounted]) => ({
    name, title: titles.get(name) || name, rawByPublic: mounted.rawByPublic,
  }))
}

/** The agent-scoped listeners: the bounded assembly wait, the ask gate and
 * the result scrub. Installed ONCE per agent in its setup. */
export function installMcpSeam(agentCtx: Context, entry: McpSeamSite): void {
  // The bounded wait + fresh tool projection (see module ARCH). The rewrite
  // re-projects `ctx.tools.schemas(scope)` sorted by name — orderTools' own
  // default order (the lore home configures no toolOrder).
  agentCtx.on('system-prompt/assemble', (assembly: unknown, context: { scope?: unknown }, next: () => Promise<any>) => {
    const waits = entry.mcpPending.splice(0)
    if (waits.length === 0) {
      if (entry.mcpMounted.size === 0) return next()
      return next().then((assembled: { tools: Array<{ name: string; description: string }> }) =>
        ({ ...assembled, tools: describeMcpTools(assembled.tools, describedServers(entry)) }))
    }
    const bound = new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, MCP_MOUNT_WAIT_MS)
      timer.unref?.()
    })
    return (async () => {
      await Promise.race([Promise.all(waits), bound])
      const fresh = await next()
      try {
        const tools = [...agentCtx.tools.schemas(context?.scope)]
        tools.sort((a: { name: string }, b: { name: string }) =>
          a.name < b.name ? -1 : a.name > b.name ? 1 : 0)
        return { ...fresh, tools: describeMcpTools(tools, describedServers(entry)) }
      } catch (err) {
        console.error(`[lore-mcp] tool refresh failed: ${String(err)}`)
        return fresh
      }
    })()
  })

  agentCtx.on('tools/pre-execute', (exec: { name?: unknown }, next: () => Promise<any>) => {
    const name = String(exec?.name ?? '')
    if (!name.startsWith('mcp__')) return next()
    const prep = entry.current
    if (!prep) return next()
    // The server's own mark, the definer's auto_approve (operator rulings)
    // or an existing session grant.
    for (const mounted of entry.mcpMounted.values()) {
      if (mounted.marksSet?.has(name)) return next()
    }
    if (owningServer(name, prep.mcpServers)?.auto_approve) return next()
    if (hasLoreGrant(prep.standingCtx.loreSessionId, name)) return next()
    return Promise.resolve({
      kind: 'ask',
      reason: `MCP tool ${name} can change data in an external service under your own key — it needs your approval`,
    })
  })

  agentCtx.on('tools/post-execute', (
    exec: { name?: unknown },
    result: { isError?: boolean; value?: unknown; content?: Array<{ type?: string; text?: unknown }> },
    next: () => Promise<any>,
  ) => {
    const name = String(exec?.name ?? '')
    if (!name.startsWith('mcp__')) return next()
    const secrets = entry.current?.mcpServers.flatMap((server) => server.secret_values) ?? []
    if (secrets.length === 0) return next()
    return (async () => {
      const decision = await next()
      // Respect a downstream replacement; only a bare accept is scrubbed.
      if (decision?.kind !== 'accept'
          || Object.hasOwn(decision, 'value') || Object.hasOwn(decision, 'content')) {
        return decision
      }
      if (result.isError) {
        // The registry refuses a value replacement on a failed result; the
        // error's model-facing text rides the content blocks.
        return { kind: 'accept', content: scrubContent(result.content ?? [], secrets) }
      }
      return { kind: 'accept', value: scrubSecrets(result.value, secrets) }
    })()
  })
}

/** The replacement marker for every scrubbed form. */
const SECRET_MARK = '[secret]'

/** Every secret's raw, base64 and url-encoded forms, longest first (a raw
 * value that is a substring of its own encoding still scrubs whole). */
function secretForms(secrets: readonly string[]): string[] {
  const forms: string[] = []
  for (const secret of secrets) {
    if (secret === '') continue
    forms.push(secret, Buffer.from(secret, 'utf8').toString('base64'), encodeURIComponent(secret))
  }
  return forms.sort((a, b) => b.length - a.length)
}

/** Deep-replace every secret form in a JSON value with the marker. Pure:
 * returns a rebuilt tree, never mutates the (frozen) result. */
export function scrubSecrets(value: unknown, secrets: readonly string[]): unknown {
  const forms = secretForms(secrets)
  if (forms.length === 0) return value
  const walk = (node: unknown): unknown => {
    if (typeof node === 'string') return scrubString(node, forms)
    if (Array.isArray(node)) return node.map(walk)
    if (node !== null && typeof node === 'object') {
      const out: Record<string, unknown> = {}
      for (const [key, child] of Object.entries(node)) out[key] = walk(child)
      return out
    }
    return node
  }
  return walk(value)
}

/** The content-block arm of the scrub (error results): text blocks carry the
 * error text; no other block kind holds raw strings. */
function scrubContent(content: Array<{ type?: string; text?: unknown }>, secrets: readonly string[]): Array<{ type?: string; text?: unknown }> {
  const forms = secretForms(secrets)
  if (forms.length === 0) return content
  return content.map((block) => block?.type === 'text' && typeof block.text === 'string'
    ? { ...block, text: scrubString(block.text, forms) }
    : block)
}

function scrubString(text: string, forms: readonly string[]): string {
  let out = text
  for (const form of forms) {
    if (out.includes(form)) out = out.split(form).join(SECRET_MARK)
  }
  return out
}

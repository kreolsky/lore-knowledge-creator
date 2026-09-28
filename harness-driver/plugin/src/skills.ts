/**
 * The skills half — the lore-skills provider + the served-toolset filter.
 *
 * # SYSTEM: harness-driver (skills half). dsh's skill/* IS the skill
 *   vocabulary: the
 *   registry (`ctx.skills`) owns discovery, tool-skill publishes the durable
 *   catalog and serves the model-facing `skill` loader, and THIS module feeds
 *   Lore's per-turn payload skills into that machinery as a provider whose
 *   bodies are already in memory.
 *
 * # ONE PARSER: the backend
 *   ships the skill DOCUMENTS raw (the lookup — project heads, instance rows,
 *   the shipped files, the tombstoned off-switches) and parses NOTHING; the
 *   frontmatter parse + validation live HERE, against the pinned harness. The
 *   name grammar is the library's own `isSkillName` (@deepseek-ai/dsh-skill —
 *   the same grammar dsh-skill-filesystem enforces on disk skills); the
 *   frontmatter split mirrors that package's parseFrontmatter (module-local
 *   there, so the ~15-line adapter repeats the FILE FORMAT, not the
 *   validation rules — the rules have exactly one source, the library
 *   import). Catalog assembly is also ours: the overlay (project > instance
 *   > shipped — a copy shadows the layer below by name), the off-switch (a
 *   tombstoned direct child, or a disabled instance row served as a NAME,
 *   suppresses the shipped skill it names) and the served-toolset gate (a
 *   skill whose whole pack is unserved is not advertised) all need parsed
 *   names, so they run here, post-parse.
 *
 * # The served-toolset filter survives on dsh's events: a skill's pack tools
 *   are CALLABLE only after the pack loaded (personas.md: filtering only the
 *   catalog lands pack tools in the always-active core — the escalation this
 *   filter exists to prevent). Activation is detected from the dsh session
 *   log itself (the `skill` tool/call + tool/result rows ARE the persisted
 *   activation — the plugin's `registered` map is a process-lifetime union
 *   and would leak activation across sessions and lose it on restart).
 *
 * Pure: no ctx, no http — everything here is a function of its arguments.
 */

import { isSkillName, type SkillCandidate, type SkillDefinition, type SkillProvider } from '@deepseek-ai/dsh-skill'
// js-yaml, not the `yaml` lib dsh-skill-filesystem uses: pnpm does not hoist
// `yaml` to the workspace root, so it does not resolve from /dsh/lore-driver
// (build-time tests AND runtime). The split only needs tolerant object/array
// discrimination of skill frontmatter (name/description/tools — no tags, no
// anchors in practice); the name GRAMMAR — the rule that matters — comes from
// the library import above, not from this module.
import jsYaml from 'js-yaml'

/** One RAW skill document from the turn payload (backend lookup — no parse). */
export interface SkillDocWire {
  location: string
  content: string
  child_docs: { id: string; title: string }[]
}

/** The payload's skills field: the raw lookup the backend walked this turn.
 * `instance` / `instance_tombstones` (the instance-settings admin layer) are
 * optional only for pre-instance payloads — the backend sends both. */
export interface SkillCatalogWire {
  project: SkillDocWire[]
  instance?: SkillDocWire[]
  shipped: SkillDocWire[]
  tombstones: string[]
  instance_tombstones?: string[]
}

/** One catalog-resolved skill: parsed metadata + the in-memory body the
 * `skill` loader serves (no Tool-API round trip — the body rode the same
 * snapshot that built the catalog, so a stale-index race cannot exist). */
export interface ResolvedSkill {
  name: string
  description: string
  tools: string[]
  location: string
  body: string
  child_docs: { id: string; title: string }[]
}

/** dsh's model-facing skill loader tool — the activation surface. */
export const SKILL_TOOL = 'skill'

/** The provider name this module registers under (Lore document skills). */
export const LORE_SKILL_PROVIDER = 'lore-skills'

/**
 * Parse one raw skill document's frontmatter. Mirrors
 * dsh-skill-filesystem's parseFrontmatter tolerance: the first line must be
 * exactly `---`, closed by a lone `---` line; the YAML must parse to a plain
 * object. `name` is required and must pass the library's grammar
 * (`isSkillName` — kebab-case, the SAME check dsh applies to on-disk skills);
 * `description` is required (trimmed, non-empty). `tools` is Lore's
 * app-extension (which tool pack the skill activates): coerced to a list of
 * non-empty strings, anything else = no pack. Returns null when the document
 * is not a skill.
 */
export function parseSkillDoc(
  content: string,
): { name: string; description: string; tools: string[]; body: string } | null {
  const raw = String(content ?? '')
  const firstLineEnd = raw.indexOf('\n')
  if (firstLineEnd < 0 || raw.slice(0, firstLineEnd).replace(/\r$/, '') !== '---') return null
  let yamlStart = firstLineEnd + 1
  let yamlEnd = -1
  let bodyStart = -1
  while (yamlStart <= raw.length) {
    const nextNewline = raw.indexOf('\n', yamlStart)
    const lineEnd = nextNewline < 0 ? raw.length : nextNewline
    if (raw.slice(yamlStart, lineEnd).replace(/\r$/, '') === '---') {
      yamlEnd = yamlStart
      bodyStart = nextNewline < 0 ? raw.length : nextNewline + 1
      break
    }
    if (nextNewline < 0) return null
    yamlStart = nextNewline + 1
  }
  if (yamlEnd < 0) return null
  const body = raw.slice(bodyStart).trim()
  let data: unknown
  try {
    data = jsYaml.load(raw.slice(firstLineEnd + 1, yamlEnd))
  } catch {
    return null
  }
  if (typeof data !== 'object' || data === null || Array.isArray(data)) return null
  const fm = data as Record<string, unknown>
  const name = fm.name
  const description = fm.description
  if (typeof name !== 'string' || !name || !isSkillName(name)) return null
  if (typeof description !== 'string' || !description.trim()) return null
  const rawTools = fm.tools
  const tools = Array.isArray(rawTools)
    ? rawTools.filter((t): t is string => typeof t === 'string' && !!t)
    : []
  return { name, description: description.trim(), tools, body }
}

/**
 * Parse one wire layer into resolved skills, skipping unparseable documents.
 * The layer loops (project, instance, shipped) were the same nine lines; the
 * shipped caller keeps ITS skip predicate (name already taken / tombstoned)
 * at the call site — it filters the parsed result, not the parse.
 */
function parseLayer(docs: SkillDocWire[] | undefined): ResolvedSkill[] {
  const out: ResolvedSkill[] = []
  for (const doc of docs ?? []) {
    const parsed = parseSkillDoc(doc?.content)
    if (!parsed) continue
    out.push({
      name: parsed.name, description: parsed.description, tools: parsed.tools,
      location: String(doc.location ?? ''), body: parsed.body,
      child_docs: Array.isArray(doc.child_docs) ? doc.child_docs : [],
    })
  }
  return out
}

/**
 * The catalog: parse the payload's raw lookup, then apply — in this order —
 * the overlay (project > instance > shipped: a copy shadows the layer below
 * by name ENTIRELY — head + pack), the off-switch (a tombstoned direct
 * child's parsed name, or a disabled instance row's NAME, suppresses the
 * shipped skill it names; an unparseable project tombstone suppresses
 * nothing) and the served-toolset gate (a skill with a non-empty pack is
 * advertised only when at least one member is served — an EMPTY pack is a
 * prose-only skill and is always relevant). Project skills first in subtree
 * order, then surviving instance skills in row order, then surviving shipped
 * skills in file order.
 *
 * INVARIANT(off-switch): a shipped skill whose project copy the user DELETED
 * stays gone. Why: deleting the document is how a user turns a shipped skill
 * off, so re-adding it from the shipped layer would leave no off-switch at
 * all. Machine-retired copies are detached from the folder by the seeded-
 * skills-overlay migration and never reach `tombstones`. The instance
 * tombstone (a disabled instance_skills row) is the ADMIN's half of the same
 * switch: served as a NAME list, not a body, so nothing can fail to parse.
 */
export function resolveSkillCatalog(
  wire: SkillCatalogWire, servedNames: Set<string>,
): ResolvedSkill[] {
  const project = parseLayer(wire.project)
  const instance = parseLayer(wire.instance)
  const tombstoneNames = new Set<string>()
  for (const raw of wire.tombstones ?? []) {
    const parsed = parseSkillDoc(raw)
    if (parsed) tombstoneNames.add(parsed.name)
  }
  const instanceTombstoneNames = new Set<string>(
    (wire.instance_tombstones ?? [])
      .map((n) => String(n ?? ''))
      .filter(Boolean),
  )
  const out = [...project]
  const takenNames = new Set(project.map((s) => s.name))
  for (const skill of instance) {
    if (takenNames.has(skill.name)) continue
    out.push(skill)
    takenNames.add(skill.name)
  }
  for (const skill of parseLayer(wire.shipped)) {
    if (
      takenNames.has(skill.name)
      || tombstoneNames.has(skill.name)
      || instanceTombstoneNames.has(skill.name)
    ) continue
    out.push(skill)
  }
  // WHY `some`, not `every`: a pack stays advertised while ANY of its tools is
  // served — with web search `off` the web-search/deep-research packs still
  // reach the model through sandbox_bash (curl). Web access in some form is the
  // baseline the operator expects, so a partial pack beats a hidden one.
  return out.filter((s) => !s.tools.length || s.tools.some((t) => servedNames.has(t)))
}

/**
 * A skill FILE is a child whose title is a relative path under `scripts/` or
 * `references/` (the grammar of backend models.SKILL_FILE_PATH_RE — the
 * Agent Skills bundle layout). The loader runs in the plugin, which has no
 * SFTP, so the body can only SAY where the files are: `## Files` lists them
 * and one fixed sentence names the tool that materializes them.
 */
export const SKILL_FILE_TITLE_RE = /^(scripts|references)\/[A-Za-z0-9_.-]+(\/[A-Za-z0-9_.-]+)?$/
export const SKILL_FILES_SENTENCE =
  'Files of this skill — materialized by `sandbox_fetch_skill` (pass this ' +
  'skill\'s name); the result names the directory; run from there:'

/**
 * The body the `skill` loader serves: the head body — plus, for a
 * folder-shaped skill, the child index (each material document's title + id,
 * so the model can read_document the ones the head references) and the
 * `## Files` listing for path-titled children — wrapped in
 * the `<skill name location>` directive block. Byte-identical to the deleted
 * Python frame_skill_body/render_skill_body pair: a skill body is
 * INSTRUCTIONS, and the directive block keeps the content framed as
 * authoritative inside dsh's own <skill_content> rendering.
 */
export function frameSkillBody(skill: ResolvedSkill): string {
  let body = skill.body
  const files = skill.child_docs.filter((c) => SKILL_FILE_TITLE_RE.test(c.title || ''))
  const material = skill.child_docs.filter((c) => !SKILL_FILE_TITLE_RE.test(c.title || ''))
  if (material.length) {
    const listing = material
      .map((c) => `- ${c.title || c.id} (${c.id})`)
      .join('\n')
    body = `${body}\n\n## Material\n\nChild documents of this skill — ` +
      `read_document them by id as the instructions above direct:\n${listing}`
  }
  if (files.length) {
    const listing = files.map((c) => `- ${c.title}`).join('\n')
    body = `${body}\n\n## Files\n\n${SKILL_FILES_SENTENCE}\n${listing}`
  }
  return `<skill name="${skill.name}" location="${skill.location}">\n\n${body}\n</skill>`
}

/**
 * The lore-skills provider: the resolved catalog is the payload; the body of
 * each skill is already in memory (it rode the same snapshot that built the
 * catalog — there is no fetch and therefore nothing to propagate or drop on
 * the load path). `get` resolves undefined only for a name outside the
 * catalog (dsh refuses those driver-side anyway) or an empty body.
 */
export function loreSkillProvider(skills: ResolvedSkill[]): SkillProvider {
  const byName = new Map(skills.map((s) => [s.name, s]))
  return {
    name: LORE_SKILL_PROVIDER,
    async list() {
      return skills.map((s) => ({
        name: s.name,
        description: s.description,
        invocation: { modelInvocable: true, userInvocable: true },
        source: 'project',
        provider: LORE_SKILL_PROVIDER,
        rank: 250,
        locator: s.location,
      })) satisfies SkillCandidate[]
    },
    async get(candidate) {
      const skill = byName.get(candidate.name)
      if (!skill) return undefined
      const content = frameSkillBody(skill)
      if (content === '') return undefined
      return {
        name: skill.name,
        description: skill.description,
        invocation: { modelInvocable: true, userInvocable: true },
        source: 'project',
        provider: LORE_SKILL_PROVIDER,
        content,
      } satisfies SkillDefinition
    },
  }
}

/** The harness's own search tool (dsh tool-web) — never in the backend payload. */
export const WEB_SEARCH_TOOL = 'web_search'

/**
 * The names the catalog's served gate counts: the payload's tools plus
 * `web_search` when the harness serves it (`webSearchServed` — the same
 * provider-pin test as tool-web's `search` in cordis.patch.yml).
 * WHY a separate set, not a wider `servedNames`: web_search is not a
 * registered proxy, so the restriction can neither deny nor grant it — it is
 * always callable. Folding it into core/active would log it as pruned while
 * the model can still call it.
 */
export function catalogServedNames(servedNames: Set<string>, webSearchServed: boolean): Set<string> {
  return webSearchServed ? new Set([...servedNames, WEB_SEARCH_TOOL]) : servedNames
}

/** The always-active CORE set: every served tool EXCEPT the union of all
 * SERVED skill pack tools (an unserved pack tool is not pruned — it is not
 * served anyway). Order-preserving. */
export function computeCoreTools(servedNames: Set<string>, skills: ResolvedSkill[]): string[] {
  const packNames = new Set<string>()
  for (const s of skills) {
    for (const t of s.tools ?? []) {
      if (servedNames.has(t)) packNames.add(t)
    }
  }
  return [...servedNames].filter((n) => !packNames.has(n))
}

/**
 * The ACTIVE set — ONE formula for both consumers (the turn's active-tools
 * log and the restrict deny-set): the core plus every ACTIVATED skill's pack
 * tools, clipped to the SERVED names. The dsh `skill` loader needs no forcing
 * here: it is a harness-native tool, never a registered proxy, so the deny
 * list (registered ∖ active) cannot touch it.
 */
export function computeActiveTools(
  servedNames: Set<string>, skills: ResolvedSkill[], activated: Set<string>,
): string[] {
  const active = new Set(computeCoreTools(servedNames, skills))
  for (const s of skills) {
    if (!activated.has(s.name)) continue
    for (const t of s.tools ?? []) active.add(t)
  }
  return [...active].filter((n) => servedNames.has(n))
}

/**
 * The scoped `tools.restrict({deny})` list — the EXACT complement of the
 * active set in `registered`, nothing hand-rolled beside it: denying
 * SUBTRACTS, so an activated skill's pack tools leave the deny-set even while
 * an INACTIVE skill still lists them (three shipped packs share
 * `sandbox_bash`; unioning the inactive packs kept it denied after one
 * activation). Unregistered names stay out (restrict fails loud on unknowns,
 * so config drift must be pre-filtered rather than kill the turn), and
 * unserved-but-registered proxies land here because the active set is
 * served-clipped — a name this turn never served is never active, whatever
 * `registered` (a process-lifetime union across sessions) still carries.
 *
 * INVARIANT(security): deny = registered ∖ computeActiveTools(served, …).
 * Why: `tools.restrict` hides a tool from the prompt AND rejects its
 * execution, and this list is the enforcement half of the served-toolset
 * filter — any second, independently derived notion of "active" (the turn
 * log drifted this way) grants or denies tools silently.
 *
 * Sorted for deterministic logs.
 */
export function restrictDenyNames(
  registered: Set<string>, served: Set<string>, skills: ResolvedSkill[],
  activated: Set<string>,
): string[] {
  const active = new Set(computeActiveTools(served, skills, activated))
  return [...registered].filter((n) => !active.has(n)).sort()
}

/** A tool/call event's skill-load identity, or null. `arguments` is the
 * lossless-JSON STRING dsh carries; a malformed payload yields null (never
 * a crash on model output). */
export function skillLoadCallName(ev: any): { callId: string; name: string } | null {
  if (ev?.type !== 'tool/call' || ev.data?.name !== SKILL_TOOL) return null
  const callId = String(ev.data.callId ?? '')
  if (!callId) return null
  try {
    const args = JSON.parse(String(ev.data.arguments ?? '{}')) as Record<string, unknown>
    const name = typeof args?.name === 'string' ? args.name : ''
    return name ? { callId, name } : null
  } catch {
    return null
  }
}

/**
 * Resolve a tool/result event against the pending skill loads.
 *
 * SUCCESS = the body was delivered: dsh's `skill` tool renders the loaded
 * definition as the result's text, so a non-empty text on a non-error result
 * IS the delivery (a failed load lands as an error result — no activation:
 * tools visible with no instructions is a divergence, not a gift). The
 * pending entry is CONSUMED either way, so a late duplicate result cannot
 * double-activate.
 */
export function skillLoadResultName(ev: any, pending: Map<string, string>): string | null {
  if (ev?.type !== 'tool/result') return null
  const block = ev?.data?.message?.content?.[0]
  const callId = String(ev?.data?.message?.source?.callId ?? block?.toolCallId ?? '')
  if (!callId || !pending.has(callId)) return null
  const name = pending.get(callId)!
  pending.delete(callId)
  if (block?.isError) return null
  const inner = Array.isArray(block?.content) ? block.content : []
  const text = inner
    .map((b: any) => (typeof b === 'string' ? b : b?.type === 'text' ? String(b.text ?? '') : ''))
    .join('')
  return text ? name : null
}

/** Restore the session's activated skill names from its event log: every
 * `skill` load round-trip that DELIVERED a body, intersected with the CURRENT
 * payload's skills (a skill document deleted mid-session drops out — the
 * restored set is a function of the live catalog, not a stale memory). */
export function restoreActivatedSkills(events: any[], payloadSkills: ResolvedSkill[]): Set<string> {
  const payloadNames = new Set(payloadSkills.map((s) => s.name))
  const pending = new Map<string, string>()
  const activated = new Set<string>()
  for (const ev of Array.isArray(events) ? events : []) {
    const call = skillLoadCallName(ev)
    if (call) {
      pending.set(call.callId, call.name)
      continue
    }
    const name = skillLoadResultName(ev, pending)
    if (name && payloadNames.has(name)) activated.add(name)
  }
  return activated
}

/** The served/core/active delta the turn logs:
 * served-minus-active IS the activation
 * mechanism's observable — the cheapest permanent proof the backend served
 * the pack. */
export function skillsDelta(
  served: Set<string>, core: string[], active: string[],
): { served: number; core: number; active: number; pruned: string[] } {
  return {
    served: served.size,
    core: core.length,
    active: active.length,
    pruned: [...served].filter((n) => !active.includes(n)).sort(),
  }
}

/**
 * Normalize the lore-turn prompt section's text (string or block list). The
 * skills index concatenation is GONE (step 8): dsh's tool-skill publishes
 * the durable catalog as its own session message — the handed-over prompt is
 * the COMPLETE section, verbatim.
 */
export function assembleTurnPrompt(systemPrompt: unknown): string {
  if (typeof systemPrompt === 'string') return systemPrompt
  if (Array.isArray(systemPrompt)) {
    return systemPrompt
      .map((b: any) => (typeof b === 'string' ? b : b?.type === 'text' ? String(b.text ?? '') : ''))
      .join('')
  }
  return ''
}

/**
 * The skills half (plan collapse-agent-stack-onto-dsh-vocabulary step 8;
 * catalog resolution from the raw lookup since collapse-the-editor-harness-layer
 * step 3): dsh's skill/* owns the catalog and the model-facing `skill` loader;
 * this module parses the payload's raw skill documents (ONE parser — the name
 * grammar is the library's isSkillName), resolves the served catalog (overlay,
 * admin off-switch, served gate) and keeps the served-toolset filter (activation = a
 * `skill` load round-trip on the dsh session log). The <available_skills> index
 * rendering is GONE — dsh's tool-skill publishes the durable catalog.
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  assembleTurnPrompt, catalogServedNames, computeActiveTools, computeCoreTools, disabledPackTools,
  frameSkillBody, parseSkillEntries,
  loreSkillProvider, LORE_SKILL_PROVIDER, parseSkillDoc, resolveSkillCatalog,
  restrictDenyNames, restoreActivatedSkills, SKILL_FILES_SENTENCE, skillEntryMatches,
  skillLoadCallName, skillLoadResultName, skillsDelta, splitVisibleSkills, withheldPackTools,
  type ResolvedSkill,
} from '../src/skills.ts'

function skill(name: string, tools: string[], location = `doc-${name}`): ResolvedSkill {
  return {
    name, description: `the ${name} skill`, tools, location,
    body: `body of ${name}`, child_docs: [],
  }
}

// ─── parseSkillDoc: the ONE frontmatter parser (name grammar = the library) ──

function doc(frontmatter: string, body = 'Fetch a URL or run a web query.'): string {
  return `---\n${frontmatter}---\n\n${body}`
}

test('a valid frontmatter doc parses to name/description/tools and a stripped body', () => {
  const parsed = parseSkillDoc(doc(
    'name: web-search\ndescription: use when the user asks to search the web.\ntools:\n  - web_search\n',
  ))
  assert.ok(parsed)
  assert.equal(parsed.name, 'web-search')
  assert.equal(parsed.description, 'use when the user asks to search the web.')
  assert.deepEqual(parsed.tools, ['web_search'])
  assert.equal(parsed.body, 'Fetch a URL or run a web query.')
})

test('absent frontmatter is not a skill; a stray --- mid-document does not open one', () => {
  assert.equal(parseSkillDoc('Just some organizational notes, no frontmatter.'), null)
  assert.equal(parseSkillDoc('intro\n---\nname: x\n'), null)
})

test('the closing --- must be a LONE line (dsh findClosingFrontmatter parity)', () => {
  // A body containing a --- line does not close the frontmatter early.
  const parsed = parseSkillDoc(
    '---\nname: a\ndescription: d\n---\nbody with\n---\nrule\n',
  )
  assert.ok(parsed)
  assert.equal(parsed.body, 'body with\n---\nrule')
  // Frontmatter never closed → not a skill.
  assert.equal(parseSkillDoc('---\nname: a\ndescription: d\n'), null)
})

test('name validation is the LIBRARY grammar: kebab-case, no doubles, no edges', () => {
  assert.ok(parseSkillDoc(doc('name: web-search\ndescription: d\n')))
  assert.ok(parseSkillDoc(doc('name: a\ndescription: d\n')))
  assert.equal(parseSkillDoc(doc('name: Web-Search\ndescription: d\n')), null, 'uppercase')
  assert.equal(parseSkillDoc(doc('name: -leading\ndescription: d\n')), null, 'leading hyphen')
  assert.equal(parseSkillDoc(doc('name: trailing-\ndescription: d\n')), null, 'trailing hyphen')
  assert.equal(parseSkillDoc(doc('name: double--hyphen\ndescription: d\n')), null, 'doubled hyphen')
  assert.equal(parseSkillDoc(doc('name: has space\ndescription: d\n')), null, 'space')
  assert.equal(parseSkillDoc(doc('name: ""\ndescription: d\n')), null, 'empty')
})

test('description is required (trimmed); tools are optional and coerced', () => {
  assert.equal(parseSkillDoc(doc('name: nodesc\n')), null)
  const noTools = parseSkillDoc(doc('name: summarize\ndescription: how to summarize.\n'))
  assert.ok(noTools)
  assert.deepEqual(noTools.tools, [])
  const badTools = parseSkillDoc(doc('name: summarize\ndescription: d\ntools: web_search\n'))
  assert.ok(badTools)
  assert.deepEqual(badTools.tools, [], 'a non-list tools is no pack')
  const dropped = parseSkillDoc(doc('name: t\ndescription: d\ntools:\n  - ""\n  - ok\n'))
  assert.ok(dropped)
  assert.deepEqual(dropped.tools, ['ok'], 'empty strings drop from the pack')
  assert.equal(parseSkillDoc(doc('name: t\ndescription: "   "\n')), null, 'blank description')
})

test('CRLF line endings parse (editor round-trip tolerance)', () => {
  const parsed = parseSkillDoc(
    '---\r\nname: crlf\ndescription: d\r\n---\r\n\r\nbody\r\n',
  )
  assert.ok(parsed)
  assert.equal(parsed.name, 'crlf')
  assert.equal(parsed.body, 'body')
})

test('invalid YAML frontmatter is not a skill (tolerant, never a crash)', () => {
  assert.equal(parseSkillDoc('---\nname: [unclosed\ndescription: d\n---\nbody'), null)
  assert.equal(parseSkillDoc('---\n- a list\n---\nbody'), null, 'a list is not frontmatter')
})

// ─── resolveSkillCatalog: overlay + admin off-switch + served gate ───────────

const WIRE = (
  project: any[] = [], shipped: any[] = [],
  instance: any[] = [], instanceTombstones: string[] = [],
) =>
  ({ project, shipped, instance, instance_tombstones: instanceTombstones })

test('project skills first, then shipped; subtree and file order preserved', () => {
  const out = resolveSkillCatalog(WIRE(
    [
      { location: 'doc-a', content: doc('name: proj-a\ndescription: d\n') },
      { location: 'doc-b', content: doc('name: proj-b\ndescription: d\n') },
    ],
    [
      { location: 'shipped:b', content: doc('name: ship-b\ndescription: d\n') },
      { location: 'shipped:a', content: doc('name: ship-a\ndescription: d\n') },
    ],
  ), new Set())
  assert.deepEqual(out.map((s) => s.name), ['proj-a', 'proj-b', 'ship-b', 'ship-a'])
  assert.deepEqual(out.map((s) => s.location), ['doc-a', 'doc-b', 'shipped:b', 'shipped:a'])
})

test('a project copy shadows the shipped skill of the same name ENTIRELY', () => {
  const out = resolveSkillCatalog(WIRE(
    [{ location: 'override', content: doc('name: web-search\ndescription: override\n', 'Project guidance.') }],
    [
      { location: 'shipped:ws', content: doc('name: web-search\ndescription: shipped\n', 'Shipped body.') },
      { location: 'shipped:other', content: doc('name: other\ndescription: d\n') },
    ],
  ), new Set())
  assert.deepEqual(out.map((s) => s.name), ['web-search', 'other'])
  assert.equal(out[0]!.location, 'override')
  assert.equal(out[0]!.body, 'Project guidance.')
})

test('the instance layer sits BETWEEN project and shipped (project > instance > shipped)', () => {
  const out = resolveSkillCatalog(WIRE(
    [{ location: 'doc-p', content: doc('name: shared\ndescription: project wins\n', 'Project body.') }],
    [
      { location: 'shipped:shared', content: doc('name: shared\ndescription: shipped\n', 'Shipped body.') },
      { location: 'shipped:base', content: doc('name: base\ndescription: d\n') },
    ],
    [
      { location: 'instance:shared', content: doc('name: shared\ndescription: instance\n', 'Instance body.') },
      { location: 'instance:extra', content: doc('name: extra\ndescription: d\n') },
    ],
  ), new Set())
  assert.deepEqual(out.map((s) => s.name), ['shared', 'extra', 'base'])
  assert.equal(out[0]!.location, 'doc-p', 'the project copy shadows the instance copy of the same name')
  assert.equal(out[1]!.location, 'instance:extra', 'a surviving instance skill serves in the middle layer')
})

test('an instance copy shadows the shipped skill of the same name (the admin override)', () => {
  const out = resolveSkillCatalog(WIRE(
    [],
    [{ location: 'shipped:ws', content: doc('name: web-search\ndescription: shipped\n', 'Shipped body.') }],
    [{ location: 'instance:web-search', content: doc('name: web-search\ndescription: instance\n', 'Instance body.') }],
  ), new Set())
  assert.deepEqual(out.map((s) => s.name), ['web-search'])
  assert.equal(out[0]!.location, 'instance:web-search')
  assert.equal(out[0]!.body, 'Instance body.')
})

test('an instance TOMBSTONE (a disabled instance row, served as a NAME list) turns the shipped skill OFF', () => {
  const out = resolveSkillCatalog(WIRE(
    [],
    [
      { location: 'shipped:ws', content: doc('name: web-search\ndescription: d\n') },
      { location: 'shipped:sb', content: doc('name: sandbox\ndescription: d\n') },
    ],
    [],
    ['web-search'],
  ), new Set())
  assert.deepEqual(out.map((s) => s.name), ['sandbox'])
})

test('instance tombstones suppress SHIPPED skills only — never project or instance ones', () => {
  const out = resolveSkillCatalog(WIRE(
    [{ location: 'p', content: doc('name: web-search\ndescription: d\n') }],
    [{ location: 'shipped:ws', content: doc('name: web-search\ndescription: d\n') }],
    [{ location: 'instance:hello', content: doc('name: hello\ndescription: d\n') }],
    ['web-search', 'hello'],
  ), new Set())
  assert.deepEqual(out.map((s) => s.name), ['web-search', 'hello'])
  assert.equal(out[0]!.location, 'p', 'a project copy always wins')
})

test('a deleted project document never turns a shipped skill off — only the admin does', () => {
  // A wire from an older backend still carrying deleted-copy bodies: they
  // are ignored, the shipped skill keeps serving.
  const out = resolveSkillCatalog({
    ...WIRE([], [
      { location: 'shipped:ws', content: doc('name: web-search\ndescription: d\n') },
      { location: 'shipped:sb', content: doc('name: sandbox\ndescription: d\n') },
    ]),
    tombstones: [doc('name: web-search\ndescription: the deleted copy\n')],
  } as Parameters<typeof resolveSkillCatalog>[0], new Set())
  assert.deepEqual(out.map((s) => s.name), ['web-search', 'sandbox'])
})

test('unparseable documents (heads and shipped files) are skipped, never fatal', () => {
  const out = resolveSkillCatalog(WIRE(
    [
      { location: 'notes', content: 'prose only' },
      { location: 'doc-a', content: doc('name: real\ndescription: d\n') },
    ],
    [{ location: 'shipped:x', content: 'also prose' }],
  ), new Set())
  assert.deepEqual(out.map((s) => s.name), ['real'])
})

test('the served gate: a non-empty pack advertises only when a member is served', () => {
  const wire = WIRE([], [
    { location: 'shipped:ws', content: doc('name: web-search\ndescription: d\ntools:\n  - web_search\n') },
    { location: 'shipped:sb', content: doc('name: sandbox\ndescription: d\ntools:\n  - sandbox_bash\n') },
    { location: 'shipped:prose', content: doc('name: prose\ndescription: d\n') },
    { location: 'shipped:mixed', content: doc('name: mixed\ndescription: d\ntools:\n  - ghost\n  - sandbox_bash\n') },
  ])
  const served = new Set(['sandbox_bash'])
  const out = resolveSkillCatalog(wire, served)
  assert.deepEqual(out.map((s) => s.name), ['sandbox', 'prose', 'mixed'],
    'web-search drops (whole pack unserved), prose stays (empty pack), mixed stays (one member served)')
  // Empty served set: only prose skills survive.
  assert.deepEqual(resolveSkillCatalog(wire, new Set()).map((s) => s.name), ['prose'])
})

// ─── frameSkillBody: byte-parity with the deleted Python pair ────────────────

test('a flat skill frames to the <skill name location> directive block', () => {
  const framed = frameSkillBody({ ...skill('web-search', [], 's1'), body: 'Fetch a URL. Cite the source.' })
  assert.equal(
    framed,
    '<skill name="web-search" location="s1">\n\nFetch a URL. Cite the source.\n</skill>',
  )
})

test('a folder skill appends the Material child index (untitled children list their id)', () => {
  const framed = frameSkillBody({
    ...skill('folder-skill', [], 'head'),
    body: 'Head instructions.',
    child_docs: [{ id: 'm1', title: 'Cheatsheet' }, { id: 'm2', title: '' }],
  })
  assert.ok(framed.startsWith('<skill name="folder-skill" location="head">'))
  assert.ok(framed.includes('Head instructions.\n\n## Material\n\n'))
  assert.ok(framed.includes('- Cheatsheet (m1)'))
  assert.ok(framed.includes('- m2 (m2)'))
  assert.ok(framed.trimEnd().endsWith('</skill>'))
})

test('path-titled children render under ## Files, the rest under ## Material', () => {
  const framed = frameSkillBody({
    ...skill('repo-stats', ['sandbox_bash'], 'head'),
    body: 'Head instructions.',
    child_docs: [
      { id: 's1', title: 'Spec' },
      { id: 'f1', title: 'scripts/run.py' },
      { id: 'f2', title: 'references/api.md' },
    ],
  })
  const material = framed.slice(framed.indexOf('## Material'), framed.indexOf('## Files'))
  assert.ok(material.includes('- Spec (s1)'))
  assert.ok(!material.includes('scripts/run.py'))
  const files = framed.slice(framed.indexOf('## Files'))
  assert.ok(files.includes(SKILL_FILES_SENTENCE))
  assert.ok(files.includes('sandbox_fetch_skill'))
  assert.ok(files.includes('\n- scripts/run.py\n- references/api.md'))
  assert.ok(!files.includes('(f1)'))
  assert.ok(framed.trimEnd().endsWith('</skill>'))
})

test('a skill with only file children has no ## Material section', () => {
  const framed = frameSkillBody({
    ...skill('only-files', [], 'head'),
    child_docs: [{ id: 'f1', title: 'scripts/run.py' }],
  })
  assert.ok(!framed.includes('## Material'))
  assert.ok(framed.includes('## Files'))
})

// ─── the lore-skills provider: the catalog is resolved; bodies are in memory ─

test('the provider lists the resolved skills as model-invocable candidates', async () => {
  const provider = loreSkillProvider([skill('web-search', ['web_search'])])
  const listed = await provider.list()
  assert.equal(listed.length, 1)
  assert.equal(listed[0]!.name, 'web-search')
  assert.equal(listed[0]!.provider, LORE_SKILL_PROVIDER)
  assert.deepEqual(listed[0]!.invocation, { modelInvocable: true, userInvocable: true })
})

test('the provider serves the FRAMED body from memory; no fetch, no race', async () => {
  const provider = loreSkillProvider([skill('web-search', ['web_search'])])
  const listed = await provider.list()
  const got = await provider.get(listed[0]!, {})
  assert.ok(got?.content.startsWith('<skill name="web-search"'))
  assert.ok(got!.content.includes('body of web-search'))
  // A name outside the resolved catalog resolves undefined (dsh refuses those
  // driver-side anyway).
  assert.equal(await provider.get({ ...listed[0]!, name: 'gone' } as any, {}), undefined)
})

// ─── computeCoreTools: served minus the union of SERVED pack tools ───────────

test('core = served minus SERVED pack tools', () => {
  const served = new Set(['search_materials', 'consolidate_memory', 'web_search'])
  const core = computeCoreTools(served, [
    skill('memory-consolidation', ['consolidate_memory']),
    skill('web-search', ['web_search']),
  ])
  assert.deepEqual(core.sort(), ['search_materials'])
})

test('a pack tool NOT served is ignored (not subtracted, not crashed)', () => {
  const partial = new Set(['search_materials', 'consolidate_memory'])
  const core = computeCoreTools(partial, [skill('web-search', ['web_search'])])
  assert.deepEqual(core.sort(), ['consolidate_memory', 'search_materials'])
})

test('no skills → every served tool is core', () => {
  const served = new Set(['a', 'b'])
  assert.deepEqual(computeCoreTools(served, []).sort(), ['a', 'b'])
})

// TEST-WHY: refused-principal — a disabled skill's tools must be denied, not leak into the always-active core.
test('disabled skill: its pack is withheld from served, so neither core nor loadable (denied)', () => {
  const wire = WIRE([], [
    { location: 'shipped:memory', content: doc('name: memory\ndescription: d\ntools:\n  - get_memory_facts\n  - shared_tool\n') },
    { location: 'shipped:other', content: doc('name: other\ndescription: d\ntools:\n  - shared_tool\n') },
  ], [], ['memory'])
  const payload = new Set(['search_materials', 'get_memory_facts', 'shared_tool'])
  const skills = resolveSkillCatalog(wire, payload)
  assert.deepEqual(skills.map((s) => s.name), ['other'])
  const withheld = disabledPackTools(wire, skills)
  assert.deepEqual([...withheld], ['get_memory_facts'], 'a tool an enabled pack lists stays loadable')
  const served = new Set([...payload].filter((n) => !withheld.has(n)))
  assert.deepEqual(computeCoreTools(served, skills), ['search_materials'])
  const deny = restrictDenyNames(payload, served, skills, new Set(['other']))
  assert.deepEqual(deny, ['get_memory_facts'], 'the disabled pack tool is denied even with every skill active')
  // A live project copy of the disabled name overrides it — nothing withheld.
  const overridden = WIRE([{ location: 'p', content: doc('name: memory\ndescription: d\ntools:\n  - get_memory_facts\n') }],
    wire.shipped, [], ['memory'])
  assert.equal(disabledPackTools(overridden, resolveSkillCatalog(overridden, payload)).size, 0)
})

// ─── The persona's visible set: allow / deny masks (plan persona-skill-sets) ─

const names = (skills: ResolvedSkill[]) => skills.map((s) => s.name)
const MAIN = () => [
  skill('main-read', ['read_document']), skill('main-edit', ['edit_document']),
  skill('main-files', ['attach_file']), skill('main-config', ['save_skill']),
]
const REST = () => [
  skill('tables', ['create_table']), skill('secrets', ['list_secrets']),
]

// TEST-WHY: pure-logic-edge — a list entry is an exact name or a prefix before a TRAILING star; every other spelling is a (non-matching) name, in the payload parser and in the matcher alike.
test('a list entry is an exact name or a prefix before a trailing star', () => {
  assert.ok(skillEntryMatches('main-read', 'main-read'))
  assert.ok(skillEntryMatches('main-read', 'main-*'))
  assert.ok(skillEntryMatches('web-search', '*'), 'a bare star names every skill')
  assert.ok(!skillEntryMatches('main-read', 'main'), 'a prefix without the star is a name')
  assert.ok(!skillEntryMatches('main-read', 'ma*in'), 'only a trailing star is a mask')
  assert.ok(!skillEntryMatches('web-search', 'main-*'))
  assert.deepEqual(parseSkillEntries(['main-*', 'main-read', 'main-*', '', 4, null]), ['main-*', 'main-read'])
  assert.deepEqual(parseSkillEntries('main-*'), [], 'a malformed field is no reason to fail a turn')
})

// TEST-WHY: pure-logic-edge — no lists means the whole catalog; allow keeps only its matches and names the reason of every other one.
test('no lists ⇒ the whole catalog; allow keeps its matches and hides the rest as outside allow', () => {
  const all = [...MAIN(), ...REST()]
  const open = splitVisibleSkills(all, [], [])
  assert.deepEqual(names(open.visible), names(all))
  assert.deepEqual(open.hidden, [])
  const closed = splitVisibleSkills(all, ['main-*'], [])
  assert.deepEqual(names(closed.visible), ['main-read', 'main-edit', 'main-files', 'main-config'])
  assert.deepEqual(closed.hidden.map(({ skill: s, reason }) => [s.name, reason]),
    [['tables', 'outside allow'], ['secrets', 'outside allow']])
})

// TEST-WHY: pure-logic-edge — deny beats allow for a name both reach, by exact name or by mask; one deny mask hides what several allow entries permit.
test('deny > allow: a name both lists reach is hidden, and the reason names the deny entry', () => {
  const both = splitVisibleSkills(MAIN(), ['main-*'], ['main-files'])
  assert.deepEqual(names(both.visible), ['main-read', 'main-edit', 'main-config'])
  assert.deepEqual(both.hidden.map(({ skill: s, reason }) => [s.name, reason]), [['main-files', 'deny main-files']])
  const byMask = splitVisibleSkills(MAIN(), ['main-files', 'main-read'], ['main-f*'])
  assert.deepEqual(names(byMask.visible), ['main-read'])
  assert.deepEqual(byMask.hidden.map(({ skill: s, reason }) => [s.name, reason]), [
    ['main-edit', 'outside allow'],
    ['main-files', 'deny main-f*'],
    ['main-config', 'outside allow'],
  ], 'catalog order; the deny entry is named for what it hid, the rest fell outside allow')
  const everything = splitVisibleSkills(REST(), ['tables', 'secrets'], ['*'])
  assert.deepEqual(everything.visible, [])
  assert.ok(everything.hidden.every(({ reason }) => reason === 'deny *'))
})

// TEST-WHY: refused-principal — a hidden skill's pack is withheld like a disabled one's (not core, not loadable), while a tool a visible skill also lists stays reachable.
test('a hidden skill\'s pack is withheld; a tool a visible skill also lists stays', () => {
  const { visible, hidden } = splitVisibleSkills([
    skill('web-search', ['web_search', 'sandbox_bash']),
    skill('sandbox', ['sandbox_bash', 'sandbox_view_image']),
  ], [], ['sandbox'])
  const withheld = withheldPackTools(hidden.map((h) => h.skill), visible)
  assert.deepEqual([...withheld], ['sandbox_view_image'], 'sandbox_bash is reached through web-search')
  // The turn's arithmetic with a closed persona: core stays 0, the hidden
  // packs leave the served set, and nothing can activate them back.
  const all = [...MAIN(), ...REST()]
  const persona = splitVisibleSkills(all, ['main-*'], ['main-config'])
  const payload = new Set([
    'read_document', 'edit_document', 'attach_file', 'save_skill', 'create_table', 'list_secrets'])
  const hiddenPacks = withheldPackTools(persona.hidden.map((h) => h.skill), persona.visible)
  const served = new Set([...payload].filter((n) => !hiddenPacks.has(n)))
  assert.deepEqual([...served].sort(), ['attach_file', 'edit_document', 'read_document'])
  assert.deepEqual(computeCoreTools(served, persona.visible), [], 'the core stays empty')
  // Forged activations of hidden skills lift nothing: a hidden pack's tools
  // stay denied beside the visible-but-unactivated packs' own.
  const deny = restrictDenyNames(payload, served, persona.visible, new Set(['tables', 'main-config']))
  assert.deepEqual(deny, [
    'attach_file', 'create_table', 'edit_document', 'list_secrets', 'read_document', 'save_skill'])
  const live = restrictDenyNames(
    payload, served, persona.visible, new Set(['main-read', 'main-edit', 'main-files', 'tables', 'main-config']))
  assert.deepEqual(live, ['create_table', 'list_secrets', 'save_skill'],
    'hidden tools stay denied even under a forged activation')
})

// ─── restrictDenyNames: the dsh served/inactive split ────────────────────────

test('unactivated pack tools are denied; activated ones are not', () => {
  const registered = new Set(['search_materials', 'web_search', 'consolidate_memory'])
  const deny = restrictDenyNames(registered, registered,
    [skill('web-search', ['web_search']), skill('memory-consolidation', ['consolidate_memory'])],
    new Set(['memory-consolidation']))
  assert.deepEqual(deny, ['web_search'])
})

test('names not registered are dropped (tools.restrict fails loud on unknowns)', () => {
  const registered = new Set(['web_search'])
  const deny = restrictDenyNames(registered, registered,
    [skill('drift', ['ghost_tool', 'web_search'])], new Set())
  assert.deepEqual(deny, ['web_search'])
})

test('no skills → nothing denied (every served tool active)', () => {
  assert.deepEqual(restrictDenyNames(new Set(['a']), new Set(['a']), [], new Set()), [])
})

test('a tool another session registered but THIS turn does not serve is denied', () => {
  const registered = new Set(['web_search', 'stale_proxy'])
  const served = new Set(['web_search'])
  // web_search is an UNACTIVATED pack tool here, so it denies alongside the
  // unserved stale proxy — the served clip only bounds the ACTIVE half.
  assert.deepEqual(
    restrictDenyNames(registered, served, [skill('web-search', ['web_search'])], new Set()),
    ['stale_proxy', 'web_search'],
  )
  // Activated, the pack tool frees; the stale proxy stays denied.
  assert.deepEqual(
    restrictDenyNames(registered, served, [skill('web-search', ['web_search'])], new Set(['web-search'])),
    ['stale_proxy'],
  )
})

test('a pack tool SHARED with an inactive skill is freed by activation', () => {
  const registered = new Set(['sandbox_bash', 'web_search', 'net_probe'])
  const served = new Set(['sandbox_bash', 'web_search', 'net_probe'])
  const skills = [
    skill('web-search', ['web_search', 'sandbox_bash']),
    skill('net-probe', ['net_probe', 'sandbox_bash']),
  ]
  // No activation: every pack tool denied (the shared sandbox_bash included).
  assert.deepEqual(
    restrictDenyNames(registered, served, skills, new Set()),
    ['net_probe', 'sandbox_bash', 'web_search'],
  )
  // Activating ONE frees the shared tool AND its pack sibling.
  assert.deepEqual(
    restrictDenyNames(registered, served, skills, new Set(['web-search'])),
    ['net_probe'],
  )
})

test("activation frees the shared tool; the inactive pack's other tool stays denied", () => {
  const registered = new Set(['sandbox_bash', 'web_search', 'net_probe'])
  const served = new Set(['sandbox_bash', 'web_search', 'net_probe'])
  const skills = [
    skill('web-search', ['web_search', 'sandbox_bash']),
    skill('net-probe', ['net_probe', 'sandbox_bash']),
  ]
  assert.deepEqual(
    restrictDenyNames(registered, served, skills, new Set(['net-probe'])),
    ['web_search'],
  )
})

test('an ACTIVATED skill cannot conjure an unserved pack tool back', () => {
  const registered = new Set(['web_search'])
  const served = new Set(['web_search'])
  assert.deepEqual(computeActiveTools(
    served, [skill('web-search', ['web_search', 'ghost_tool'])], new Set(['web-search']),
  ).sort(), ['web_search'])
})

test('INVARIANT: deny and active partition registered exactly — ONE formula, both consumers', () => {
  const registered = new Set(['search_materials', 'sandbox_bash', 'web_search', 'net_probe', 'stale_proxy'])
  const served = new Set(['search_materials', 'sandbox_bash', 'web_search', 'net_probe'])
  const skills = [
    skill('web-search', ['web_search', 'sandbox_bash']),
    skill('net-probe', ['net_probe']),
    skill('stale', ['stale_tool']),
  ]
  const activated = new Set(['web-search'])
  const active = computeActiveTools(served, skills, activated)
  const deny = restrictDenyNames(registered, served, skills, activated)
  for (const n of registered) {
    assert.ok(active.includes(n) !== deny.includes(n),
      `${n} must sit in exactly one of active/deny`)
  }
  assert.deepEqual([...deny, ...active].sort(), [...registered].sort())
  assert.deepEqual([...active].sort(), ['sandbox_bash', 'search_materials', 'web_search'])
  assert.deepEqual(deny, ['net_probe', 'stale_proxy'])
})

// ─── restore: the dsh session log is the activation store ────────────────────

// Real 0.2.0-rc.2 shapes (observed live in a v4 session log): tool/call
// carries arguments as a lossless-JSON STRING; tool/result is a FLAT tool
// message — role 'tool', callId/isError on the message, the RENDERED skill
// body directly in the blocks.
function call(seq: number, callId: string, name: string, args: unknown): any {
  return { seq, type: 'tool/call', data: { turn: 1, step: 1, callId, name, arguments: JSON.stringify(args) } }
}
function result(seq: number, callId: string, text: string, isError = false): any {
  return {
    seq, type: 'tool/result', data: { turn: 1, step: 1, message: {
      role: 'tool', source: { kind: 'tool', callId }, toolCallId: callId,
      content: [{ type: 'text', text }], isError,
    } }, surfaceOp: 'append',
  }
}

const SKILL_BODY = '# web search instructions\nsearch the web.'

test('a successful `skill` load round-trip restores the activation', () => {
  const events = [
    call(2, 'c1', 'skill', { name: 'web-search' }),
    result(3, 'c1', SKILL_BODY),
  ]
  const restored = restoreActivatedSkills(events, [skill('web-search', ['web_search'])])
  assert.deepEqual([...restored], ['web-search'])
})

test('a FAILED load (error result) does not activate', () => {
  const events = [
    call(2, 'c1', 'skill', { name: 'web-search' }),
    result(3, 'c1', 'skill "web-search" is unknown or no longer available', true),
  ]
  assert.deepEqual([...restoreActivatedSkills(events, [skill('web-search', ['web_search'])])], [])
})

test('an EMPTY delivered body does not activate either', () => {
  const events = [
    call(2, 'c1', 'skill', { name: 'web-search' }),
    result(3, 'c1', ''),
  ]
  assert.deepEqual([...restoreActivatedSkills(events, [skill('web-search', ['web_search'])])], [])
})

test('a skill deleted mid-session drops from the restored set', () => {
  const events = [
    call(2, 'c1', 'skill', { name: 'web-search' }),
    result(3, 'c1', SKILL_BODY),
  ]
  // The payload no longer carries web-search (doc deleted) → not restored.
  assert.deepEqual([...restoreActivatedSkills(events, [skill('other', ['x'])])], [])
})

test('a result with no matching pending call activates nothing', () => {
  const events = [result(3, 'cX', SKILL_BODY)]
  assert.deepEqual([...restoreActivatedSkills(events, [skill('web-search', ['web_search'])])], [])
})

// ─── mid-turn detectors (the turn listener wires these) ───────────────────────

test('skillLoadCallName parses a tool/call; malformed arguments yield null', () => {
  const ev = call(2, 'c1', 'skill', { name: 'web-search' })
  assert.deepEqual(skillLoadCallName(ev), { callId: 'c1', name: 'web-search' })
  const bad = { seq: 3, type: 'tool/call', data: { callId: 'c2', name: 'skill', arguments: '{not json' } }
  assert.equal(skillLoadCallName(bad), null)
  assert.equal(skillLoadCallName(call(4, 'c3', 'read_document', {})), null)
})

test('skillLoadResultName resolves success on a delivered non-error body', () => {
  const pending = new Map([['c1', 'web-search']])
  assert.equal(skillLoadResultName(result(3, 'c1', SKILL_BODY), pending), 'web-search')
  assert.equal(pending.size, 0, 'the pending entry is consumed either way')
  const pending2 = new Map([['c2', 'web-search']])
  assert.equal(skillLoadResultName(result(4, 'c2', 'unknown skill', true), pending2), null)
  assert.equal(pending2.size, 0)
})

// ─── skillsDelta + assembleTurnPrompt ─────────────────────────────────────────

test('skillsDelta mirrors the pi log keys: served/core/active/pruned', () => {
  const delta = skillsDelta(
    new Set(['a', 'b', 'c']), ['a'], ['a', 'b'],
  )
  assert.deepEqual(delta, { served: 3, core: 1, active: 2, pruned: ['c'] })
})

test('assembleTurnPrompt normalizes the handed-over prompt; no index concatenation', () => {
  assert.equal(assembleTurnPrompt('base prompt'), 'base prompt')
  assert.equal(assembleTurnPrompt([{ type: 'text', text: 'hello ' }, { type: 'text', text: 'world' }]), 'hello world')
  assert.equal(assembleTurnPrompt(undefined), '')
  // The skills payload never reaches the section text (dsh's catalog owns it).
  assert.equal(assembleTurnPrompt('base prompt'), 'base prompt')
})

// ─── catalogServedNames: the harness's own web_search counts for the gate ────

test('a web_search pack is always advertised — the tool has no off state', () => {
  // Web search is always served to the model (a provider without a key fails
  // its calls loudly); the served gate must never prune the packs that need it.
  const wire = WIRE([], [{ location: 'shipped:ws', content: doc('name: web-search\ndescription: d\ntools:\n  - web_search\n  - sandbox_bash\n') }])
  const payloadTools = new Set(['search_materials'])
  assert.deepEqual(resolveSkillCatalog(wire, catalogServedNames(payloadTools)).map((s) => s.name), ['web-search'])
})

test('catalogServedNames leaves the payload set untouched', () => {
  const payloadTools = new Set(['search_materials'])
  catalogServedNames(payloadTools)
  assert.deepEqual([...payloadTools], ['search_materials'])
})

/**
 * Proof the ARTIFACTS run, not just their sources: plain node, no tsx, no
 * tsconfig paths, no workspace on the resolution path — close to the
 * conditions the browser gives them (smoke-hooks.mjs supplies the two things
 * node lacks: css modules and Lore's externals). A bundle that only works
 * next to packages/ would pass conversation.test.ts and fail on the first
 * page load.
 *
 * Also asserts neither bundle reaches for a node builtin: one would be
 * invisible here (node has them) and fatal in a browser.
 */
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { register } from 'node:module'

import { createLoreConversation } from '../dist/lore-conversation.js'

register('./smoke-hooks.mjs', import.meta.url)
const { MarkdownText } = await import('../dist/lore-markdown.js')

const dist = join(import.meta.dirname, '..', 'dist')

// Exactly the three sealed outputs: a fourth file here means rolldown split a
// shared or lazy chunk that sync-bundle.sh (one cat per artifact) would
// silently drop from the frontend tree.
const produced = readdirSync(dist).sort()
const expected = ['lore-conversation.js', 'lore-markdown.css', 'lore-markdown.js']
if (produced.join(',') !== expected.join(',')) {
  console.error(`dist holds ${produced.join(', ')} — expected exactly ${expected.join(', ')}`)
  process.exit(1)
}

for (const file of ['lore-conversation.js', 'lore-markdown.js']) {
  const source = readFileSync(join(dist, file), 'utf8')
  const builtins = [...new Set(source.match(/node:[a-z_]+/g) ?? [])]
  if (builtins.length > 0) {
    console.error(`${file} imports node builtins, unusable in a browser: ${builtins.join(', ')}`)
    process.exit(1)
  }
}

// The lazy grammar imports must SURVIVE as bare specifiers: sealed grammars
// (~1.6 MB) are exactly what neverBundle exists to keep out of the bundle.
const markdownSource = readFileSync(join(dist, 'lore-markdown.js'), 'utf8')
if (!/import\(["']@shikijs\/langs\//.test(markdownSource)) {
  console.error('lore-markdown.js sealed or rewrote the lazy @shikijs/langs imports')
  process.exit(1)
}

if (MarkdownText?.$$typeof !== Symbol.for('react.memo') || typeof MarkdownText.type !== 'function') {
  console.error('lore-markdown.js does not export a memoized MarkdownText component')
  process.exit(1)
}

const inputs = []
for (const line of readFileSync(join(import.meta.dirname, 'fixtures', 'session-turn.jsonl'), 'utf8').split('\n')) {
  if (line.trim() === '') continue
  const row = JSON.parse(line)
  if (row.seq === undefined) continue
  inputs.push({ type: 'event', event: row })
}

const conversation = createLoreConversation()
conversation.replaceWindow(inputs, false)
const nodes = conversation.nodes()
if (nodes.length === 0) {
  console.error('the bundle assembled no nodes from a real session log')
  process.exit(1)
}
console.log(`bundle ok: ${nodes.length} nodes, kinds ${[...new Set(nodes.map(n => n.kind))].join(', ')}`)

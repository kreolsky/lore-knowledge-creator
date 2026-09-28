/**
 * Loader hooks for bundle-smoke.mjs: the two conditions plain node does NOT
 * give the lore-markdown bundle that a browser does.
 *
 * 1. Stylesheets: node cannot execute `.css`; the browser hands them to its
 *    css engine. The hook returns an empty module for every `.css` — the
 *    sheet's bytes are not this smoke's subject (they are committed as
 *    frontend/src/dsh/lore-markdown.css and asserted by dsh-pin-gate.py).
 * 2. The bundle's externalized bare imports (react, katex, shiki,
 *    @shikijs/*): in the browser Vite resolves them from Lore's own flat
 *    node_modules; the pnpm analog of that flattened graph is the hidden
 *    hoist store (node_modules/.pnpm/node_modules), which holds every
 *    dependency of every workspace package — including transitive ones like
 *    @shikijs/core that no single package's node_modules can satisfy. The
 *    anchor is a real file in the store (react/package.json), so the REAL
 *    libraries load, not stubs.
 */
const EXTERNAL_RE = /^(?:react|react-dom|katex|shiki)(?:\/|$)|^@shikijs\//

const externalBase = 'file:///dsh/node_modules/.pnpm/node_modules/react/package.json'

export async function resolve(specifier, context, nextResolve) {
  if (EXTERNAL_RE.test(specifier)) {
    return nextResolve(specifier, { ...context, parentURL: externalBase })
  }
  return nextResolve(specifier, context)
}

export async function load(url, context, nextLoad) {
  if (url.endsWith('.css')) {
    return { format: 'module', shortCircuit: true, source: 'export default {}' }
  }
  return nextLoad(url, context)
}

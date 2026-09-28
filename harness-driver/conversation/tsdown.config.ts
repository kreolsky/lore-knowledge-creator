import { readFileSync } from 'node:fs'
import { dirname, resolve as resolvePath } from 'node:path'
import { defineConfig } from 'tsdown'
import { transform } from 'lightningcss'

/**
 * Two headless ESM files for the browser, both sealing dsh workspace packages
 * inside: `lore-conversation` (the assembler and its node definitions) and
 * `lore-markdown` (the assistant-Markdown renderer).
 *
 * Delivery is a bundle rather than an npm install because the peer graph is
 * ~16 `workspace:^` packages and the client runtime pins a zustand major below
 * Lore's; npm's published rc is not the sha this image runs. Node builtins stay
 * external on purpose — a builtin reaching the bundle means a path that cannot
 * run in a browser, and it must fail at import rather than be shimmed.
 *
 * lore-markdown additionally externalizes react/react-dom/katex/shiki: the
 * renderer runs on Lore's own React 19 and katex, and the lazy shiki grammar
 * imports must reach Vite as bare specifiers so it can split them into async
 * chunks (sealing them would put ~1.6 MB into one file `cat`-extracted by
 * sync-bundle.sh). The current `alwaysBundle: [/^[a-z]/]` alone would seal all
 * of those — neverBundle wins over alwaysBundle in tsdown.
 */

/** Virtual-id wrapper keeping module CSS away from rolldown's raw css handling (no @tsdown/css in the workspace). */
const CSS_VIRTUAL_PREFIX = '\0lore-css:'
const CSS_VIRTUAL_SUFFIX = '.mjs'

/** The slice of the rolldown plugin context the stylesheet plugin uses. */
interface PluginContext {
  addWatchFile(file: string): void
  emitFile(file: { type: 'asset'; fileName: string; source: string }): string
}

/**
 * Compile `.module.css` imports of ONE entry into a hashed-classmap default
 * export plus a single `<name>.css` asset beside the entry's js. Modeled on
 * dsh's own `dsh-css-modules-inline` preset (same lightningcss transform,
 * same `[hash]_[local]` pattern), but emitting the sheet as an asset instead
 * of a runtime `<style>` injection: Lore commits the css next to the bundle
 * and the wrapper imports it.
 */
function cssModulesAsset(name: string) {
  const sheets: string[] = []
  return {
    name: 'lore-css-modules-asset',
    resolveId(source: string, importer: string | undefined) {
      if (!source.endsWith('.module.css')) return null
      const abs = importer !== undefined ? resolvePath(dirname(importer), source) : resolvePath(source)
      return CSS_VIRTUAL_PREFIX + abs + CSS_VIRTUAL_SUFFIX
    },
    async load(this: unknown, virtualId: string) {
      const ctx = this as PluginContext
      if (!virtualId.startsWith(CSS_VIRTUAL_PREFIX)) return null
      const fileId = virtualId.slice(CSS_VIRTUAL_PREFIX.length, -CSS_VIRTUAL_SUFFIX.length)
      ctx.addWatchFile(fileId)
      // lightningcss is the workspace root's own devDependency and the exact
      // mechanism dsh's tsdown.client.ts preset uses for these same files.
      const { code, exports: cssExports } = transform({
        filename: fileId,
        code: readFileSync(fileId),
        cssModules: { pattern: '[hash]_[local]' },
        minify: true,
      })
      sheets.push(code.toString())
      const classMap: Record<string, string> = {}
      for (const [local, exp] of Object.entries(cssExports ?? {})) classMap[local] = exp.name
      return `export default ${JSON.stringify(classMap)};`
    },
    generateBundle(this: unknown, _options: unknown, bundle: Readonly<Record<string, unknown>>) {
      const ctx = this as PluginContext
      if (sheets.length === 0) return
      if (bundle[`${name}.js`] === undefined) {
        throw new Error(`css-modules plugin: entry output ${name}.js missing from the bundle`)
      }
      ctx.emitFile({ type: 'asset', fileName: `${name}.css`, source: sheets.join('\n') })
    },
  }
}

export default defineConfig({
  entry: {
    'lore-conversation': 'src/index.ts',
    'lore-markdown': 'src/markdown.ts',
  },
  outDir: 'dist',
  format: ['esm'],
  platform: 'neutral',
  target: 'es2024',
  fixedExtension: false,
  dts: false,
  clean: true,
  deps: {
    alwaysBundle: [/^@deepseek-ai\//, /^[a-z]/],
    neverBundle: [
      /^node:/,
      // lore-markdown's externals: Lore's own runtime copies, never sealed.
      /^react(\/|$)/,
      /^react-dom(\/|$)/,
      /^katex(\/|$)/,
      /^shiki/,
      /^@shikijs\//,
    ],
  },
  plugins: [cssModulesAsset('lore-markdown')],
})

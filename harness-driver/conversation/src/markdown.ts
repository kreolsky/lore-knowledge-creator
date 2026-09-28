/**
 * dsh's untrusted assistant-Markdown renderer for the browser — the chat
 * panel's rendering engine (CommonMark+GFM+TeX via the mdast/micromark
 * pipeline, KaTeX, shiki highlighting, incremental streaming).
 *
 * Bundled as a SECOND sealed entry beside `lore-conversation` (same delivery:
 * this directory is copied to /dsh/lore-conversation inside the harness image,
 * which already builds the whole dsh workspace, so the renderer and the loop
 * are the same sha by construction). `react`, `react-dom`, `katex`, `shiki`
 * and `@shikijs/*` stay EXTERNAL here: the sealed file imports Lore's own
 * copies (no second React instance), and the lazy `@shikijs/langs/…` grammar
 * imports survive as bare specifiers, so Vite turns them into its own async
 * chunks instead of sealing ~1.6 MB of grammars into one file. The renderer's
 * CSS Modules compile to one `lore-markdown.css` asset emitted next to this
 * bundle (see the plugin in tsdown.config.ts).
 */

// WHY: relative into the workspace, same form as src/index.ts — mapped package
// specifiers would pull the compiled client barrel; the source tree is the
// cordis-free path to the renderer.
export { MarkdownText } from '../../packages/client/ui-primitives/src/markdown/MarkdownText.tsx'

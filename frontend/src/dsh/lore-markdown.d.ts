/**
 * Typed surface of the committed markdown renderer bundle (lore-markdown.js,
 * beside this file; its compiled stylesheet is lore-markdown.css, imported by
 * the wrapper). The bundle is BUILT inside the harness image from
 * `harness-driver/conversation/src/markdown.ts` — dsh's MarkdownText over the
 * mdast/micromark GFM+TeX pipeline — and committed here (plan
 * chat-markdown-on-dsh step 1); this hand-kept declaration pins the surface
 * Lore consumes and keeps tsc out of the sealed artifact. It is a build-time
 * coupling: the Vite build fails loudly when the .js is missing — there is no
 * stale-copy fallback. react/katex/shiki inside the bundle resolve to Lore's
 * own installs (see frontend/package.json), never to a sealed second copy.
 */
import type { MemoExoticComponent, ReactNode } from 'react'

/** Localized fence copy-button labels. */
export interface MarkdownCodeLabels {
  copyLabel: string
  copiedLabel: string
}

/** Localized chrome for a Markdown document; pass a reference-stable object (memoized per locale) — a new identity discards the streaming render cache mid-message. */
export interface MarkdownLabels {
  code: MarkdownCodeLabels
  /** Screen-reader heading of the footnote section. */
  footnotes: string
}

export interface MarkdownTextProps {
  /** Markdown source text preserved by the session projection. */
  readonly text: string
  /** Renders fences and TeX plain while streaming; KaTeX and highlighting land on the settled swap. */
  readonly streaming?: boolean
  readonly labels: MarkdownLabels
}

export declare const MarkdownText: MemoExoticComponent<(props: MarkdownTextProps) => ReactNode>

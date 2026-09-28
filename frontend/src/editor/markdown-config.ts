/**
 * Shared `@codemirror/lang-markdown` extensions used by all CM6 instances.
 *
 * Currently disables setext-style headings (`text\n===` / `text\n---`) so that
 * a lone `-` typed under a paragraph stays a bullet draft instead of being
 * re-parsed as an h2 underline that swallows the paragraph above.
 */
// ARCH: parser-name 'SetextHeading' is the @lezer/markdown internal id (v1.x).
// If a future major bump renames it, setext headings will silently come back.
import type { MarkdownExtension } from '@lezer/markdown';

export const markdownExtensions: MarkdownExtension[] = [
  { remove: ['SetextHeading'] },
];

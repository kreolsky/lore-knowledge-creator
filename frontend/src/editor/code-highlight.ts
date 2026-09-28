/**
 * Syntax highlighting for fenced code blocks (YAML, JSON, etc.).
 *
 * ARCH: Uses `class` instead of `color` in HighlightStyle.define() to prevent
 * color leakage into regular markdown text. The Lezer markdown parser reuses
 * tags like `tags.content` (Paragraph) and `tags.string` (LinkTitle), so
 * setting `color` directly would style ALL document text, not just code blocks.
 *
 * Instead, CSS classes (tok-code-*) are scoped to `.cm-fenced-code` in theme.ts,
 * so colors only apply inside fenced code blocks. The YAML parser is loaded
 * dynamically by @codemirror/language-data when a fenced block has language
 * tag yaml/yml.
 */

import { HighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { tags } from '@lezer/highlight';
import type { Extension } from '@codemirror/state';

export const codeHighlightStyle: Extension = syntaxHighlighting(
  HighlightStyle.define([
    { tag: tags.propertyName, class: 'tok-code-key' },
    { tag: tags.string, class: 'tok-code-string' },
    { tag: tags.content, class: 'tok-code-plain-scalar' },
    { tag: tags.number, class: 'tok-code-number' },
    { tag: tags.integer, class: 'tok-code-number' },
    { tag: tags.float, class: 'tok-code-number' },
    { tag: tags.bool, class: 'tok-code-keyword' },
    { tag: tags.null, class: 'tok-code-keyword' },
    { tag: tags.comment, class: 'tok-code-comment' },
    { tag: tags.typeName, class: 'tok-code-type' },
    { tag: tags.variableName, class: 'tok-code-key' },
    { tag: tags.meta, class: 'tok-code-comment' },
    { tag: tags.keyword, class: 'tok-code-keyword' },
    { tag: tags.operator, class: 'tok-code-keyword' },
  ]),
);

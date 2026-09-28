/**
 * Markdown → HTML for the CLIPBOARD: `cleanMarkdownToHtml` emits class-free
 * semantic HTML (no classes, no buttons, no in-app tagging) so a copy from the
 * editor pastes with formatting into Word / Google Docs / Gmail / Notion.
 * Symmetric copy-side counterpart to the paste handler's HTML→Markdown
 * (Turndown).
 *
 * The chat panel no longer renders through this pipeline — it renders dsh's
 * sealed MarkdownText (components/chat/MarkdownContent.tsx, plan
 * chat-markdown-on-dsh). Replacing this regex pipeline with mdast-util-to-hast
 * is a separate task.
 *
 * SYSTEM: markdown-to-html — clipboard MD→HTML pipeline (cleanMarkdownToHtml); chat renders dsh's MarkdownText.
 *
 * ARCH: the stash/placeholder mechanism protects island content. "HTML islands"
 * (code/mermaid/math/tables) are replaced with `\uE000{index}\uE000` placeholders
 * BEFORE the regex passes (tables, blockquotes, lists, math, code, headings,
 * emphasis, links, line-breaks), then restored last — their content never flows
 * through a later regex pass.
 */
import { escapeHtml } from '../../utils/html';
import { parseInAppLink } from '../../utils/in-app-link';

// Private-use sentinel for stashed-island placeholders — cannot appear in markdown text.
const MARK = '\uE000';

// Reverses escapeHtml for the one consumer that needs source characters back (math source).
const UNESCAPE: Record<string, string> = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'" };
function unescapeHtml(s: string): string {
  return s.replace(/&(?:amp|lt|gt|quot|#39);/g, (m) => UNESCAPE[m]);
}

// ─── Cell / table helpers ─────────────────────────────────────────────────────

/** Inline formatting (bold/italic/strike/code) for a single table cell. Cell text arrives escaped (see runPipeline). */
function applyCellInline(text: string): string {
  return text
    .replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/~~(.+?)~~/g, '<del>$1</del>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

function parseTableRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|')) s = s.slice(0, -1);
  return s.split('|').map((c) => c.trim());
}

// GFM separator row: pipe-delimited cells of dashes with optional alignment colons.
const TABLE_SEP_RE = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

/** Detect GFM tables (header + `|---|` separator + body) and stash them as rendered HTML. */
function renderTables(text: string, stash: (html: string) => string): string {
  const lines = text.split('\n');
  const out: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const next = lines[i + 1];
    if (line.includes('|') && next !== undefined && next.includes('-') && TABLE_SEP_RE.test(next)) {
      const header = parseTableRow(line);
      const body: string[][] = [];
      let j = i + 2;
      while (j < lines.length && lines[j].includes('|') && lines[j].trim() !== '') {
        body.push(parseTableRow(lines[j]));
        j++;
      }
      const thead = `<thead><tr>${header.map((c) => `<th>${applyCellInline(c)}</th>`).join('')}</tr></thead>`;
      const tbody = `<tbody>${body
        .map((r) => `<tr>${r.map((c) => `<td>${applyCellInline(c)}</td>`).join('')}</tr>`)
        .join('')}</tbody>`;
      out.push(stash(`<table>${thead}${tbody}</table>`));
      i = j - 1;
      continue;
    }
    out.push(line);
  }
  return out.join('\n');
}

// ─── Blockquotes ──────────────────────────────────────────────────────────────

// Quote line: `> content`, a bare `>`, or `> ` (empty) — the marker is already HTML-escaped
// (`&gt;`) by the time this pass runs (see runPipeline).
const QUOTE_LINE_RE = /^&gt;(?: |$)/;

// INVARIANT: consecutive `>` lines form ONE blockquote; an empty `>` line is a paragraph
// break *inside* that quote, not a quote terminator.
// Why: matches CommonMark blockquote continuation — treating an empty `>` as a terminator
// would split a single quote into several.
function renderBlockquotes(text: string): string {
  const lines = text.split('\n');
  const out: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    if (!QUOTE_LINE_RE.test(lines[i])) {
      out.push(lines[i]);
      continue;
    }
    const quoted: string[] = [];
    while (i < lines.length && QUOTE_LINE_RE.test(lines[i])) {
      quoted.push(lines[i].replace(/^&gt; ?/, ''));
      i++;
    }
    i--; // step back; the for-loop's i++ resumes after the quote block
    out.push(`<blockquote>${quoted.join('<br/>')}</blockquote>`);
  }
  return out.join('\n');
}

// ─── Lists ────────────────────────────────────────────────────────────────────

// List item: optional leading indent, a bullet (-, *, +) or ordered (1.) marker, then content.
const LIST_ITEM_RE = /^(\s*)([-*+]|\d+\.)\s+(.+)$/;

interface ListItem { indent: number; ordered: boolean; content: string; }

// Task marker at the START of a list item's content: `[ ]`/`[x]`/`[X]` then the
// remaining content. Tolerant of extra spaces after the marker (LLM-emitted).
const TASK_PREFIX_RE = /^\[([ xX])\]\s+(.*)$/;

/** If a list item's content starts with a task marker, render a disabled
 *  checkbox; otherwise return the content unchanged.
 *  m[2] is already escaped (runPipeline escapes the whole text up front), so
 *  injected markup in task content can't break out of the <li>. */
function renderTaskCheckbox(content: string): string {
  const m = TASK_PREFIX_RE.exec(content);
  if (!m) return content;
  const checked = m[1] === 'x' || m[1] === 'X';
  return `<input type="checkbox" data-task="${checked ? 1 : 0}"${checked ? ' checked' : ''} disabled /> ${m[2]}`;
}

// INVARIANT: nesting level is derived from leading-whitespace width (deeper = more
// spaces) via a stack of open lists; a nested list lives INSIDE its parent <li>.
// Why: HTML rule — closing the parent <li> before its nested list would produce
// invalid HTML the browser re-parents unpredictably, breaking rendered nesting;
// the parent <li> stays open until we dedent past it.
function buildNestedList(items: ListItem[]): string {
  let html = '';
  const stack: Array<{ indent: number; tag: 'ul' | 'ol' }> = [];
  for (const item of items) {
    const tag = item.ordered ? 'ol' : 'ul';
    const content = renderTaskCheckbox(item.content);
    if (stack.length === 0 || item.indent > stack[stack.length - 1].indent) {
      html += `<${tag}><li>${content}`;
      stack.push({ indent: item.indent, tag });
      continue;
    }
    // Same or shallower: close deeper lists (but never the base) until indents line up.
    while (stack.length > 1 && item.indent < stack[stack.length - 1].indent) {
      html += `</li></${stack[stack.length - 1].tag}>`;
      stack.pop();
    }
    html += `</li><li>${content}`;
  }
  while (stack.length > 0) {
    html += `</li></${stack[stack.length - 1].tag}>`;
    stack.pop();
  }
  return html;
}

/** Convert contiguous runs of list lines (with indentation-based nesting) into <ul>/<ol> HTML. */
function renderLists(text: string): string {
  const lines = text.split('\n');
  const out: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    if (!LIST_ITEM_RE.test(lines[i])) {
      out.push(lines[i]);
      continue;
    }
    const items: ListItem[] = [];
    let m: RegExpExecArray | null;
    while (i < lines.length && (m = LIST_ITEM_RE.exec(lines[i])) !== null) {
      items.push({
        indent: m[1].replace(/\t/g, '    ').length,
        ordered: /\d/.test(m[2]),
        content: m[3],
      });
      i++;
    }
    i--; // step back; the for-loop's i++ resumes after the list block
    out.push(buildNestedList(items));
  }
  return out.join('\n');
}

// ─── Emission ─────────────────────────────────────────────────────────────────

/** Fenced-code renderer: class-free <pre><code class="language-X">. */
function cleanCodeRenderer(raw: string, lang: string): string {
  const cls = lang ? ` class="language-${lang}"` : '';
  return `<pre><code${cls}>${escapeHtml(raw)}</code></pre>`;
}

/** Math renderer: literal escaped source, delimiters preserved ("$…$ stays as text"). */
function cleanMathRenderer(src: string, display: boolean): string {
  return escapeHtml(display ? `$$${src}$$` : `$${src}$`);
}

// ─── Block layout (clipboard) ──────────────────────────────────────────────────

// A line that is ALREADY a block element by the time the layout pass runs: heading, list,
// blockquote (code/tables/math are stashed placeholders, handled separately below).
const BLOCK_LINE_RE = /^<(?:h[1-4]|ul|ol|blockquote|pre|table)\b/;

/**
 * Block-layout pass: fold the working text into block-level HTML. Blank lines
 * are paragraph boundaries; a lone `\n` inside a prose run is a soft break (`<br/>`). Heading /
 * list / blockquote lines and lone stash placeholders (table/code/math islands) are emitted
 * standalone; every other run of prose lines is wrapped in a single `<p>`.
 *
 * WHY: clipboard `text/html` carries real paragraphs — Google Docs/Word get true
 * `<p>`, and the Lore→Lore round trip (`<p>`→`\n\n` in Turndown) stays byte-clean.
 * A lone `\n` maps to `<br/>` (not a GFM space) on purpose, to survive the
 * Turndown hard-break round trip — accepts a minor divergence from strict GFM
 * for copy stability.
 */
function foldBlocks(text: string, lonePlaceholder: RegExp): string {
  const out: string[] = [];
  let para: string[] = [];
  const flush = (): void => {
    if (para.length === 0) return;
    out.push(`<p>${para.join('<br/>')}</p>`);
    para = [];
  };
  for (const line of text.split('\n')) {
    const trimmed = line.trim();
    if (trimmed === '') {
      flush();
      continue;
    }
    if (BLOCK_LINE_RE.test(line) || lonePlaceholder.test(trimmed)) {
      flush();
      out.push(line);
      continue;
    }
    para.push(line);
  }
  flush();
  return out.join('');
}

// ─── The pipeline ─────────────────────────────────────────────────────────────

function runPipeline(md: string): string {
  const tokens: string[] = [];
  const stash = (html: string): string => {
    const i = tokens.length;
    tokens.push(html);
    return `${MARK}${i}${MARK}`;
  };

  let html = md
    // Mermaid fenced code blocks — the clipboard keeps the fenced source.
    .replace(/```mermaid\n([\s\S]*?)```/g, (_match, code: string) =>
      stash(cleanCodeRenderer(code.trimEnd(), 'mermaid')),
    )
    // Fenced code blocks (non-mermaid).
    .replace(/```(\w*)\n([\s\S]*?)```/g, (_match, lang: string, code: string) =>
      stash(cleanCodeRenderer(code.trimEnd(), lang)),
    );

  // INVARIANT(security): every character of the source that is not a fenced island is
  // HTML-escaped HERE, once, before any pass emits a tag; no later pass escapes its slice
  // again (double escaping) and none may skip it. Why: the output lands in a clipboard
  // `text/html` payload — markup in the source must never reach the pasted DOM verbatim
  // from plain prose, the one place the per-construct escapes (link text, code, cells,
  // task items) did not cover.
  html = escapeHtml(html);

  // GFM tables (line-based) — before block/inline passes so cells aren't mangled
  html = renderTables(html, stash);
  // Blockquotes (line-based) — merge consecutive `>` lines into one quote
  html = renderBlockquotes(html);
  // Lists (line-based) — indentation-aware nesting; BEFORE emphasis so a leading "* " bullet
  // is never parsed as italic.
  html = renderLists(html);

  // Images: http(s) → <img>; everything else (internal schemes, blocklisted)
  // → visible alt text. Runs before the link pass so `![alt](url)` isn't
  // turned into `!` + anchor.
  html = html.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (_match, alt: string, url: string) => {
    if (/^https?:\/\//i.test(url.trim())) {
      return stash(`<img src="${url}" alt="${alt}">`);
    }
    return alt;
  });

  html = html
    // Block math $$\n...\n$$ (multi-line)
    // Math sources are unescaped back to their characters, then escaped by the
    // renderer as literal source.
    .replace(/^\$\$[ \t]*\n([\s\S]+?)\n[ \t]*\$\$$/gm, (_match, latex: string) =>
      stash(cleanMathRenderer(unescapeHtml(latex), true)),
    )
    // Single-line display math $$...$$
    .replace(/\$\$(.+?)\$\$/g, (_match, latex: string) =>
      stash(cleanMathRenderer(unescapeHtml(latex), true)),
    )
    // Inline math $...$ — before emphasis so $a*b*c$ isn't eaten by italics
    .replace(/(?<!\$)\$(?!\$)(?!\s)(.+?)(?<!\s|\$)\$(?!\$)/g, (_match, latex: string) =>
      stash(cleanMathRenderer(unescapeHtml(latex), false)),
    )
    // Inline code (already escaped, stashed so emphasis can't touch it)
    .replace(/`([^`]+)`/g, (_match, code: string) => stash(`<code>${code}</code>`))
    // Headings (must be before bold since # could be confused)
    .replace(/^#### (.+)$/gm, '<h4>$1</h4>')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    // Bold + italic
    .replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    // Strikethrough
    .replace(/~~(.+?)~~/g, '<del>$1</del>')
    // Links — protocol blocklist (XSS gate); internal schemes reduce to visible
    // text; external http(s) → target=_blank rel=noopener.
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_match, text: string, url: string) => {
      const trimmed = url.trim();
      if (/^(javascript|data|vbscript):/i.test(trimmed)) return text;
      if (parseInAppLink(trimmed)) return text;
      return `<a href="${url}" target="_blank" rel="noopener">${text}</a>`;
    });

  // Block layout: real paragraphs. A lone stash placeholder (table/code/math
  // island) on its own line is a standalone block, never wrapped in <p>.
  html = foldBlocks(html, new RegExp(`^${MARK}\\d+${MARK}$`));

  // Restore stashed HTML islands
  html = html.replace(new RegExp(`${MARK}(\\d+)${MARK}`, 'g'), (_match, i: string) => tokens[Number(i)]);

  return html;
}

// ─── Entry point ──────────────────────────────────────────────────────────────

/**
 * Clipboard clean entry: class-free semantic HTML for `text/html` clipboard payloads.
 * No classes, no copy buttons, no in-app data-* tagging; internal-scheme links
 * and non-http images render as visible text; external http(s) links/images stay
 * active; math renders as literal escaped source; mermaid renders as a fenced
 * code block.
 */
export function cleanMarkdownToHtml(md: string): string {
  return runPipeline(md);
}

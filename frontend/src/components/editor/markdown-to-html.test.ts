// @vitest-environment jsdom
/**
 * Tests for the clipboard markdown→HTML module.
 *
 * SYSTEM: markdown-to-html — covers `cleanMarkdownToHtml` (clipboard entry) semantics:
 * the CLEAN mode emits class-free semantic HTML and collapses internal-scheme
 * links/images to visible text.
 */

import { describe, it, expect } from 'vitest';
import { cleanMarkdownToHtml } from './markdown-to-html';

describe('cleanMarkdownToHtml — formatting constructs (class-free semantic HTML)', () => {
  it('renders headings h1–h4', () => {
    const html = cleanMarkdownToHtml('# H1\n## H2\n### H3\n#### H4');
    expect(html).toContain('<h1>H1</h1>');
    expect(html).toContain('<h2>H2</h2>');
    expect(html).toContain('<h3>H3</h3>');
    expect(html).toContain('<h4>H4</h4>');
  });

  it('renders bold / italic / bold+italic / strikethrough', () => {
    const html = cleanMarkdownToHtml('**b** *i* ***bi*** ~~s~~');
    expect(html).toContain('<strong>b</strong>');
    expect(html).toContain('<em>i</em>');
    expect(html).toContain('<strong><em>bi</em></strong>');
    expect(html).toContain('<del>s</del>');
  });

  it('renders inline code without a class', () => {
    const html = cleanMarkdownToHtml('use `x()` now');
    expect(html).toContain('<code>x()</code>');
    expect(html).not.toContain('chat-inline-code');
  });

  it('renders a fenced code block as a bare <pre><code language-X> with no copy button', () => {
    const html = cleanMarkdownToHtml('```js\nconst a = 1;\n```');
    expect(html).toContain('<pre><code class="language-js">');
    expect(html).toContain('const a = 1;');
    expect(html).not.toContain('chat-code');
    expect(html).not.toContain('data-chat-code');
    expect(html).not.toContain('chat-code-copy-btn');
    expect(html).not.toContain('data-needs-hl');
  });

  it('renders a fenced code block with no lang as bare <code>', () => {
    const html = cleanMarkdownToHtml('```\nplain\n```');
    expect(html).toContain('<pre><code>plain</code></pre>');
    expect(html).not.toContain('language-');
  });

  it('renders mermaid as a fenced code block of the source', () => {
    const html = cleanMarkdownToHtml('```mermaid\ngraph TD; A-->B;\n```');
    expect(html).toContain('<pre><code class="language-mermaid">');
    expect(html).toContain('A--&gt;B;');
    expect(html).not.toContain('chat-mermaid');
    expect(html).not.toContain('data-mermaid');
  });

  it('renders a GFM table as a class-free <table>', () => {
    const html = cleanMarkdownToHtml('| A | B |\n|---|---|\n| 1 | 2 |');
    expect(html).toContain('<table>');
    expect(html).not.toContain('chat-table');
    expect(html).toContain('<th>A</th>');
    expect(html).toContain('<td>1</td>');
  });

  it('applies inline formatting inside clean table cells', () => {
    const html = cleanMarkdownToHtml('| **b** | `c` |\n|---|---|\n| *i* | ~~s~~ |');
    expect(html).toContain('<strong>b</strong>');
    expect(html).toContain('<em>i</em>');
    expect(html).toContain('<del>s</del>');
    expect(html).toContain('<code>c</code>');
  });

  it('renders a single-line blockquote', () => {
    expect(cleanMarkdownToHtml('> quote')).toContain('<blockquote>quote</blockquote>');
  });

  it('merges consecutive blockquote lines into one quote', () => {
    const html = cleanMarkdownToHtml('> a\n>\n> b');
    expect((html.match(/<blockquote>/g) || []).length).toBe(1);
    expect(html).toContain('a<br/><br/>b');
  });

  it('renders ordered + unordered lists', () => {
    // A blank line separates the two list blocks (contiguous list lines form ONE block).
    const html = cleanMarkdownToHtml('1. a\n2. b\n\n- x\n- y');
    expect(html).toContain('<ol><li>a</li><li>b</li></ol>');
    expect(html).toContain('<ul><li>x</li><li>y</li></ul>');
  });

  it('builds nested lists from indentation', () => {
    expect(cleanMarkdownToHtml('* L1\n    * L2')).toContain(
      '<ul><li>L1<ul><li>L2</li></ul></li></ul>',
    );
  });

  it('renders a task item as a CLASSLESS disabled checkbox', () => {
    const html = cleanMarkdownToHtml('- [ ] open\n- [x] done');
    expect(html).toContain('<input type="checkbox" data-task="0" disabled />');
    expect(html).toContain('<input type="checkbox" data-task="1" checked disabled />');
    expect(html).not.toContain('chat-task');
    // The task <li> carries no class in clean mode.
    expect(html).not.toContain('chat-task-item');
  });
});

describe('cleanMarkdownToHtml — links & images (portable)', () => {
  it('renders an external http link as a safe target=_blank anchor', () => {
    const html = cleanMarkdownToHtml('[example](https://example.com)');
    expect(html).toContain('<a href="https://example.com" target="_blank" rel="noopener">example</a>');
  });

  it('collapses internal-scheme links (ref:/doc:/note:) to visible text only', () => {
    const html = cleanMarkdownToHtml('[see](ref:abc) [d](doc:def) [n](note:ghi)');
    expect(html).not.toContain('href="ref:');
    expect(html).not.toContain('href="doc:');
    expect(html).not.toContain('href="note:');
    expect(html).not.toContain('<a ');
    expect(html).toContain('see');
    expect(html).toContain('d');
    expect(html).toContain('n');
  });

  it('renders an external http image as <img>', () => {
    const html = cleanMarkdownToHtml('![pic](https://x.com/a.png)');
    expect(html).toContain('<img src="https://x.com/a.png" alt="pic">');
  });

  it('collapses an internal-scheme image to alt text', () => {
    const html = cleanMarkdownToHtml('![My Table](table:abc)');
    expect(html).not.toContain('<img');
    expect(html).not.toContain('href');
    expect(html).toContain('My Table');
  });
});

describe('cleanMarkdownToHtml — math + line breaks', () => {
  it('renders math as escaped literal source (not KaTeX)', () => {
    const html = cleanMarkdownToHtml('inline $a<b$ and block $$c>d$$');
    expect(html).not.toContain('katex');
    expect(html).not.toContain('chat-math');
    expect(html).toContain('$a&lt;b$');
    expect(html).toContain('$$c&gt;d$$');
  });

  it('wraps prose blocks in <p>, single-newline → intra-paragraph <br/>', () => {
    // Block layout: blank line = paragraph boundary (<p>…</p>); a lone \n inside a
    // paragraph is a soft break (<br/>). No stray <br/><br/> between blocks.
    const html = cleanMarkdownToHtml('one\n\ntwo\nthree');
    expect(html).toBe('<p>one</p><p>two<br/>three</p>');
    expect(html).not.toContain('<br/><br/>');
  });
});

describe('cleanMarkdownToHtml — block layout (clipboard fidelity)', () => {
  it('emits a block heading followed by a <p>, no <br/> glue', () => {
    const html = cleanMarkdownToHtml('# Title\n\nbody text');
    expect(html).toBe('<h1>Title</h1><p>body text</p>');
  });

  it('separates a heading from adjacent prose even without a blank line', () => {
    const html = cleanMarkdownToHtml('# Title\nbody text');
    expect(html).toBe('<h1>Title</h1><p>body text</p>');
  });

  it('leaves a list as a standalone block (no wrapping <p>, no adjacent <br/>)', () => {
    const html = cleanMarkdownToHtml('intro\n\n- one\n- two');
    expect(html).toBe('<p>intro</p><ul><li>one</li><li>two</li></ul>');
    expect(html).not.toContain('<br/>');
  });

  it('does not wrap a standalone table placeholder in <p>', () => {
    const html = cleanMarkdownToHtml('before\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nafter');
    expect(html).toContain('<p>before</p>');
    expect(html).toContain('<table>');
    expect(html).toContain('<p>after</p>');
    // The table must not be swallowed into a paragraph.
    expect(html).not.toMatch(/<p>[^<]*<table>/);
  });
});

describe('cleanMarkdownToHtml — XSS parity', () => {
  it('blocks javascript: protocol (collapses to text)', () => {
    const html = cleanMarkdownToHtml('[click](javascript:alert(1))');
    expect(html).not.toContain('href="javascript:');
    expect(html).not.toContain('<a ');
    expect(html).toContain('click');
  });

  it('blocks data: and vbscript: protocols', () => {
    expect(cleanMarkdownToHtml('[a](data:text/html,<script>)')).not.toContain('href="data:');
    expect(cleanMarkdownToHtml('[a](vbscript:MsgBox)')).not.toContain('href="vbscript:');
  });

  it('escapes HTML inside fenced code blocks', () => {
    const html = cleanMarkdownToHtml('```\n<script>alert(1)</script>\n```');
    expect(html).not.toContain('<script>alert(1)</script>');
    expect(html).toContain('&lt;script&gt;');
  });

  it('escapes HTML inside inline code', () => {
    const html = cleanMarkdownToHtml('`<b>x</b>`');
    expect(html).not.toContain('<b>x</b>');
    expect(html).toContain('&lt;b&gt;');
  });

  it('escapes HTML inside table cells', () => {
    const html = cleanMarkdownToHtml('| <img onerror=x> | b |\n|---|---|\n| 1 | 2 |');
    expect(html).not.toContain('<img onerror=x>');
    expect(html).toContain('&lt;img');
  });
});

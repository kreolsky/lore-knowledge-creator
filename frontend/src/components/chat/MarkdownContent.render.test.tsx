/**
 * Construct tests for the dsh-backed chat markdown renderer (plan
 * chat-markdown-on-dsh, step 3): the 18-row gap table from
 * wip/2026-09-11-chat-render-gaps.md plus the wrapper's own contract
 * (internal-link rewrite + click, fence-split mermaid, fenced literals).
 *
 * The contract is the DOM, never an HTML string: each case renders
 * <MarkdownContent> with @testing-library/react and asserts elements.
 * The sealed bundle arrives through the wrapper's lazy import(), so the
 * first render settles asynchronously — the helper flushes it inside act()
 * and async bits use waitFor.
 */

import { describe, it, expect, vi, afterEach, beforeAll } from 'vitest';
import { render, waitFor, fireEvent, act, cleanup, type RenderResult } from '@testing-library/react';
import { MarkdownContent } from './MarkdownContent';
import { emit } from '../../events';

// Warm the lazy bundle BEFORE any assertion window: the first vitest import
// of the sealed renderer pays its transform, which can exceed waitFor's
// default timeout on a cold worker.
beforeAll(async () => {
  await import('../../dsh/lore-markdown');
});

vi.mock('../../events', () => ({ emit: vi.fn() }));

vi.mock('../../editor/mermaid-render', () => ({
  // A marker SVG per diagram code — enough to assert the wrapper wired the
  // fence-split mermaid path without pulling the real mermaid engine in.
  renderMermaid: vi.fn((code: string) => Promise.resolve(`<svg role="img" data-code-len="${code.length}"></svg>`)),
  getCachedMermaid: vi.fn(() => undefined),
}));

async function renderMarkdown(content: string, streaming = false): Promise<RenderResult> {
  let utils!: RenderResult;
  await act(async () => {
    utils = render(<MarkdownContent content={content} streaming={streaming} />);
  });
  // Flush the lazy bundle import and the async mermaid renders.
  await act(async () => { await new Promise(r => setTimeout(r, 0)); });
  return utils;
}

afterEach(() => {
  cleanup();
  vi.mocked(emit).mockClear();
});

describe('MarkdownContent — the 18 probed constructs render via dsh', () => {
  it('#1 a link inside table cells is a real anchor (header and body)', async () => {
    const { container } = await renderMarkdown('| [Google](https://google.com) | x |\n|---|---|\n| [Wikipedia](https://wikipedia.org) | 2 |');
    const body = await waitFor(() => {
      const a = container.querySelector<HTMLAnchorElement>('td a');
      if (!a) throw new Error('anchor in td not mounted');
      return a;
    });
    expect(body.getAttribute('href')).toBe('https://wikipedia.org');
    expect(container.querySelector('th a')?.getAttribute('href')).toBe('https://google.com');
  });

  it('#2 a loose ordered list is ONE list with three items', async () => {
    const { container } = await renderMarkdown('1. a\n\n2. b\n\n3. c');
    await waitFor(() => { if (container.querySelectorAll('ol li').length < 3) throw new Error('list not mounted'); });
    expect(container.querySelectorAll('ol')).toHaveLength(1);
    expect([...container.querySelectorAll('ol > li')].map(li => li.textContent?.trim())).toEqual(['a', 'b', 'c']);
  });

  it('#3 an ordered item keeps its continuation line', async () => {
    const { container } = await renderMarkdown('1. one\n   more text\n2. two');
    await waitFor(() => { if (!container.querySelector('ol')) throw new Error('list not mounted'); });
    expect(container.querySelectorAll('ol')).toHaveLength(1);
    const items = container.querySelectorAll('ol > li');
    expect(items).toHaveLength(2);
    expect(items[0]?.textContent).toContain('more text');
  });

  it('#4 inline \\(…\\) TeX renders through KaTeX', async () => {
    const { container } = await renderMarkdown('Value \\(E=mc^2\\) ok');
    await waitFor(() => { if (!container.querySelector('.katex')) throw new Error('katex not mounted'); });
    expect(container.textContent).toContain('E');
  });

  it('#5 display \\[…\\] TeX renders through KaTeX', async () => {
    const { container } = await renderMarkdown('\\[\nE=mc^2\n\\]');
    await waitFor(() => { if (!container.querySelector('.katex-display')) throw new Error('katex display not mounted'); });
  });

  it('#6 $ x+1 $ with inner spaces still renders math', async () => {
    const { container } = await renderMarkdown('$ x+1 $');
    await waitFor(() => { if (!container.querySelector('.katex')) throw new Error('katex not mounted'); });
  });

  it('#7 emphasis inside link text stays emphasis, not escaped markup', async () => {
    const { container } = await renderMarkdown('[**bold**](https://example.com)');
    const strong = await waitFor(() => {
      const s = container.querySelector<HTMLAnchorElement>('a[href="https://example.com"] strong');
      if (!s) throw new Error('strong inside anchor not mounted');
      return s;
    });
    expect(strong.textContent).toBe('bold');
  });

  it('#8 --- is a thematic break', async () => {
    const { container } = await renderMarkdown('a\n\n---\n\nb');
    await waitFor(() => { if (!container.querySelector('hr')) throw new Error('hr not mounted'); });
  });

  it('#9 h5 renders', async () => {
    const { container } = await renderMarkdown('##### five');
    await waitFor(() => { if (!container.querySelector('h5')) throw new Error('h5 not mounted'); });
    expect(container.querySelector('h5')?.textContent).toBe('five');
  });

  it('#10 underscore emphasis works', async () => {
    const { container } = await renderMarkdown('_it_ and __bold__');
    await waitFor(() => { if (!container.querySelector('em')) throw new Error('em not mounted'); });
    expect(container.querySelector('em')?.textContent).toBe('it');
    expect(container.querySelector('strong')?.textContent).toBe('bold');
  });

  it('#11 bare and angle autolinks become anchors', async () => {
    const bare = await renderMarkdown('see https://example.com now');
    await waitFor(() => { if (!bare.container.querySelector('a[href="https://example.com"]')) throw new Error('autolink not mounted'); });
    const angle = await renderMarkdown('<https://example.org>');
    await waitFor(() => { if (!angle.container.querySelector('a[href="https://example.org"]')) throw new Error('angle autolink not mounted'); });
  });

  it('#12 backslash escapes unescape', async () => {
    const { container } = await renderMarkdown('a \\* b');
    await waitFor(() => { if (!container.querySelector('p')) throw new Error('p not mounted'); });
    expect(container.querySelector('p')?.textContent).toBe('a * b');
  });

  it('#13 setext heading renders as h1', async () => {
    const { container } = await renderMarkdown('Title\n=====');
    await waitFor(() => { if (!container.querySelector('h1')) throw new Error('h1 not mounted'); });
    expect(container.querySelector('h1')?.textContent).toBe('Title');
  });

  it('#14 footnotes render (reference + note, no literal [^1])', async () => {
    const { container } = await renderMarkdown('text[^1]\n\n[^1]: the note body');
    await waitFor(() => { if (!container.textContent?.includes('the note body')) throw new Error('footnote not mounted'); });
    expect(container.textContent).not.toContain('[^1]');
  });

  it('#15 table cell alignment lands as inline style', async () => {
    const { container } = await renderMarkdown('| a | b |\n|:--|--:|\n| 1 | 2 |');
    await waitFor(() => { if (!container.querySelector('td')) throw new Error('table not mounted'); });
    expect(container.querySelector('td[style*="right"]')).toBeTruthy();
    expect(container.querySelector('td[style*="left"]')).toBeTruthy();
  });

  it('#16 a list inside a blockquote renders bullets', async () => {
    const { container } = await renderMarkdown('> - a\n> - b');
    const items = await waitFor(() => {
      const lis = container.querySelectorAll('blockquote ul li');
      if (lis.length < 2) throw new Error('quoted list not mounted');
      return [...lis];
    });
    expect(items.map(li => li.textContent)).toEqual(['a', 'b']);
  });

  it('#17 an absolute https image renders as <img>; an internal scheme stays alt text', async () => {
    const remote = await renderMarkdown('![pic](https://example.com/y.png)');
    await waitFor(() => { if (!remote.container.querySelector('img')) throw new Error('img not mounted'); });
    expect(remote.container.querySelector('img')?.getAttribute('src')).toBe('https://example.com/y.png');
    // doc: is not rewritten for images (no broken lore.local <img> attempts) —
    // dsh unwraps the disallowed destination to the alt text.
    const internal = await renderMarkdown('![pic](doc:abc123)');
    await waitFor(() => { if (internal.container.textContent?.includes('pic') !== true) throw new Error('alt not mounted'); });
    expect(internal.container.querySelector('img')).toBeNull();
  });

  it('#18 raw HTML in prose renders as text, never as markup', async () => {
    const { container } = await renderMarkdown('quoted: <img src=x onerror=alert(1)> end');
    await waitFor(() => { if (!container.querySelector('p')) throw new Error('p not mounted'); });
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('p')?.textContent).toContain('<img src=x onerror=alert(1)>');
  });
});

describe('MarkdownContent — the Lore wrapper contract', () => {
  it('rewrites a doc: link to the lore.local form and emits navigate-to-document on click', async () => {
    const { container } = await renderMarkdown('[the doc](doc:doc123)');
    const anchor = await waitFor(() => {
      const a = container.querySelector<HTMLAnchorElement>('a[href^="https://lore.local/l/doc/"]');
      if (!a) throw new Error('rewritten anchor not mounted');
      return a;
    });
    fireEvent.click(anchor);
    expect(emit).toHaveBeenCalledWith('navigate-to-document', { documentId: 'doc123' });
  });

  it('a ref: link click emits navigate-to-reference with stayInContext', async () => {
    const { container } = await renderMarkdown('[see ref](ref:ref456)');
    const anchor = await waitFor(() => {
      const a = container.querySelector<HTMLAnchorElement>('a[href^="https://lore.local/l/ref/"]');
      if (!a) throw new Error('rewritten anchor not mounted');
      return a;
    });
    fireEvent.click(anchor);
    expect(emit).toHaveBeenCalledWith('navigate-to-reference', { referenceId: 'ref456', stayInContext: true });
  });

  it('a mermaid fence renders an <svg> through mermaid-render', async () => {
    const { container } = await renderMarkdown('```mermaid\ngraph TD; A-->B;\n```');
    await waitFor(() => { if (!container.querySelector('.chat-mermaid svg')) throw new Error('mermaid svg not mounted'); });
  });

  it('the mermaid body excludes the closing fence (a trailing ``` became a stray node / a parse error)', async () => {
    const { renderMermaid } = await import('../../editor/mermaid-render');
    vi.mocked(renderMermaid).mockClear();
    await renderMarkdown('```mermaid\nsequenceDiagram\n    A->>B: hi\n```\n\ntail prose');
    await waitFor(() => { if (!vi.mocked(renderMermaid).mock.calls.length) throw new Error('renderMermaid not called'); });
    expect(vi.mocked(renderMermaid).mock.calls[0][0]).toBe('sequenceDiagram\n    A->>B: hi');
  });

  it('a doc: link inside a fenced code block stays literal', async () => {
    const { container } = await renderMarkdown('```js\nconst u = "[d](doc:abc)";\n```');
    await waitFor(() => { if (!container.textContent?.includes('doc:abc')) throw new Error('fence not mounted'); });
    expect(container.querySelector('a[href*="lore.local"]')).toBeNull();
  });

  it('rewrites the literal inside inline code too (recorded deviation: the lore.local form shows there)', async () => {
    const { container } = await renderMarkdown('`[d](doc:abc)`');
    await waitFor(() => {
      if (!container.textContent?.includes('https://lore.local/l/doc/abc')) throw new Error('inline rewrite not mounted');
    });
  });

  it('while streaming the live tail renders fence source plainly and settles on re-render', async () => {
    const streaming = await renderMarkdown('```js\nconst x = 1;\n```', true);
    await waitFor(() => { if (!streaming.container.textContent?.includes('const x = 1;')) throw new Error('streaming fence not mounted'); });
    const settled = await renderMarkdown('```js\nconst x = 1;\n```', false);
    await waitFor(() => { if (!settled.container.textContent?.includes('const x = 1;')) throw new Error('settled fence not mounted'); });
  });
});

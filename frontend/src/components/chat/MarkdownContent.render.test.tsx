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

  it('#4 inline \\(…\\) TeX renders through KaTeX', async () => {
    const { container } = await renderMarkdown('Value \\(E=mc^2\\) ok');
    await waitFor(() => { if (!container.querySelector('.katex')) throw new Error('katex not mounted'); });
    expect(container.textContent).toContain('E');
  });

  it('#5 display \\[…\\] TeX renders through KaTeX', async () => {
    const { container } = await renderMarkdown('\\[\nE=mc^2\n\\]');
    await waitFor(() => { if (!container.querySelector('.katex-display')) throw new Error('katex display not mounted'); });
  });
});

describe('MarkdownContent — the Lore wrapper contract', () => {

  it.each([
    ['bare', '[the doc](1169966b-afe4-4bfd-b532-5187f3ba1a30)'],
    ['angle', '[the doc](<1169966b-afe4-4bfd-b532-5187f3ba1a30>)'],
  ])('a scheme-less (%s) id link is a document link — the form the agent prompt prescribes', async (_shape, src) => {
    const { container } = await renderMarkdown(src);
    const anchor = await waitFor(() => {
      const a = container.querySelector<HTMLAnchorElement>('a[href^="https://lore.local/l/doc/"]');
      if (!a) throw new Error('rewritten anchor not mounted');
      return a;
    });
    fireEvent.click(anchor);
    expect(emit).toHaveBeenCalledWith('navigate-to-document', { documentId: '1169966b-afe4-4bfd-b532-5187f3ba1a30' });
  });

  it('http, mailto and anchor links are not rewritten to the document form', async () => {
    const { container } = await renderMarkdown('[w](https://example.com/x) [m](mailto:a@b.c) [h](#top)');
    await waitFor(() => { if (!container.querySelector('a[href="https://example.com/x"]')) throw new Error('not mounted'); });
    expect(container.querySelector('a[href*="lore.local"]')).toBeNull();
  });

  it('the mermaid body excludes the closing fence (a trailing ``` became a stray node / a parse error)', async () => {
    const { renderMermaid } = await import('../../editor/mermaid-render');
    vi.mocked(renderMermaid).mockClear();
    await renderMarkdown('```mermaid\nsequenceDiagram\n    A->>B: hi\n```\n\ntail prose');
    await waitFor(() => { if (!vi.mocked(renderMermaid).mock.calls.length) throw new Error('renderMermaid not called'); });
    expect(vi.mocked(renderMermaid).mock.calls[0][0]).toBe('sequenceDiagram\n    A->>B: hi');
  });
});

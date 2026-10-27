/** GFM table widget renders cell markdown — links included — as the editor does. */
// @vitest-environment jsdom
import { describe, it, expect, vi } from 'vitest';
import { TableWidget } from './table-widget';

vi.mock('mermaid', () => ({
  default: { initialize: vi.fn(), render: vi.fn(async () => ({ svg: '<svg></svg>' })) },
}));

function cellTexts(raw: string): string[] {
  const dom = new TableWidget(raw).toDOM();
  return [...dom.querySelectorAll('th, td')].map((c) => c.textContent ?? '');
}

describe('TableWidget', () => {
  it('collapses an external markdown link to its label', () => {
    const raw = '| Name | Link |\n|---|---|\n| tldraw | [repo](https://github.com/tldraw/tldraw) |';
    expect(cellTexts(raw)).toEqual(['Name', 'Link', 'tldraw', 'repo']);
  });

  it('collapses an internal link to its label', () => {
    const raw = '| Doc | Ref |\n|---|---|\n| [Spec](doc:abc123) | [Scan](ref:def456) |';
    expect(cellTexts(raw)).toEqual(['Doc', 'Ref', 'Spec', 'Scan']);
  });

  it('keeps inline formatting', () => {
    const raw = '| A |\n|---|\n| **bold** `code` |';
    const dom = new TableWidget(raw).toDOM();
    expect(dom.querySelector('td .cm-strong')?.textContent).toBe('bold');
    expect(dom.querySelector('td')?.textContent).toBe('bold code');
  });
});

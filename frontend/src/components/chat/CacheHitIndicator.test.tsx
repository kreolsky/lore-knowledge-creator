/** CacheHitIndicator — last-turn prompt-cache share, read off the turn-tail node. */
// @vitest-environment jsdom

import { describe, it, expect, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('../../i18n', () => ({ useTranslation: () => ({ t: (k: string, p?: Record<string, unknown>) => {
  if (p) return `${k}:${JSON.stringify(p)}`;
  return k;
} }) }));

import { CacheHitIndicator, lastTurnCacheHit } from './CacheHitIndicator';
import type { ConversationVM } from '../../store/chat-store/conversation-feed';

function tail(seq: number, tokenUsage: Record<string, unknown> | undefined): ConversationVM {
  return { key: `tail-${seq}`, kind: 'turn-tail', anchorSeq: seq, data: { turn: seq, seq, ...(tokenUsage ? { tokenUsage } : {}) } };
}
const other = (seq: number): ConversationVM => ({ key: `a-${seq}`, kind: 'assistant', anchorSeq: seq, data: {} });

describe('lastTurnCacheHit', () => {
  it('reads the LAST turn-tail, not an earlier one', () => {
    const fig = lastTurnCacheHit([
      tail(1, { uncachedInputTokens: 9000, cacheReadTokens: 0 }),
      other(2),
      tail(3, { uncachedInputTokens: 166, cacheReadTokens: 14188 }),
      other(4),
    ]);
    expect(fig).toEqual({ cacheRead: 14188, uncached: 166, pct: 99 });
  });

  it('no indicator when the provider reported no cache bucket', () => {
    expect(lastTurnCacheHit([tail(1, { uncachedInputTokens: 9450 })])).toBeNull();
    expect(lastTurnCacheHit([tail(1, undefined)])).toBeNull();
    expect(lastTurnCacheHit([other(1)])).toBeNull();
    expect(lastTurnCacheHit([])).toBeNull();
  });

  it('no indicator on an empty prompt', () => {
    expect(lastTurnCacheHit([tail(1, { uncachedInputTokens: 0, cacheReadTokens: 0 })])).toBeNull();
  });
});

function render(node: ReturnType<typeof createElement>): { container: HTMLElement; root: Root } {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => { root.render(node); });
  return { container, root };
}

function cleanup(root: Root, container: HTMLElement) {
  act(() => { root.unmount(); });
  container.remove();
}

function band(container: HTMLElement): string {
  return container.querySelector('[data-band]')?.getAttribute('data-band') ?? '';
}

describe('CacheHitIndicator', () => {
  it('renders the percentage and a tooltip with the raw counts', () => {
    const { container, root } = render(
      <CacheHitIndicator figure={{ cacheRead: 14188, uncached: 166, pct: 99 }} />,
    );
    expect(container.textContent).toContain('99%');
    expect(container.querySelector('[title]')?.getAttribute('title'))
      .toBe('chatCacheHitTip:{"cached":"14188","fresh":"166","pct":99}');
    cleanup(root, container);
  });

  it('bands: low hit rate is the alarm', () => {
    const cases: Array<[number, string]> = [[99, 'neutral'], [80, 'neutral'], [79, 'amber'], [40, 'amber'], [39, 'danger'], [0, 'danger']];
    for (const [pct, expected] of cases) {
      const { container, root } = render(<CacheHitIndicator figure={{ cacheRead: pct, uncached: 100 - pct, pct }} />);
      expect(band(container), `pct=${pct}`).toBe(expected);
      cleanup(root, container);
    }
  });
});

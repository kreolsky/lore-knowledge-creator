/** TokenUsageGauge — context-window fill indicator. */
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

import { TokenUsageGauge } from './TokenUsageGauge';

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

// The component stamps a data-band attribute on the root for testability + a11y.
function band(container: HTMLElement): string {
  const el = container.querySelector('[data-band]');
  return el?.getAttribute('data-band') ?? '';
}

describe('TokenUsageGauge', () => {
  it('renders humanized used/cap + percentage', () => {
    const { container, root } = render(<TokenUsageGauge used={12300} cap={262144} />);
    expect(container.textContent).toContain('12.3k');
    expect(container.textContent).toContain('262k'); // 262144 / 1000 → 262k
    expect(container.textContent).toContain('5%');
    cleanup(root, container);
  });

  it('neutral band below 70%', () => {
    const { container, root } = render(<TokenUsageGauge used={1000} cap={262144} />);
    expect(band(container)).toBe('neutral');
    cleanup(root, container);
  });

  it('amber band at 70–90%', () => {
    const { container: c1, root: r1 } = render(<TokenUsageGauge used={183500} cap={262144} />); // 70%
    expect(band(c1)).toBe('amber');
    cleanup(r1, c1);
    const { container: c2, root: r2 } = render(<TokenUsageGauge used={235930} cap={262144} />); // 90%
    expect(band(c2)).toBe('amber');
    cleanup(r2, c2);
  });

  it('danger band above 90%', () => {
    const { container, root } = render(<TokenUsageGauge used={250000} cap={262144} />); // ~95%
    expect(band(container)).toBe('danger');
    cleanup(root, container);
  });

  it('0% is neutral (fresh session before first turn)', () => {
    const { container, root } = render(<TokenUsageGauge used={0} cap={262144} />);
    expect(band(container)).toBe('neutral');
    expect(container.textContent).toContain('0%');
    cleanup(root, container);
  });

  it('clamps percentage at 100% when used exceeds cap', () => {
    const { container, root } = render(<TokenUsageGauge used={300000} cap={262144} />);
    expect(container.textContent).toContain('100%');
    expect(band(container)).toBe('danger');
    cleanup(root, container);
  });
});

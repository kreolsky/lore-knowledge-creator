/** ListRow — shared clamped row content. */
// @vitest-environment jsdom

import { describe, it, expect } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

import { ListRow } from './ListRow';

function render(node: ReturnType<typeof createElement>): { container: HTMLElement; root: Root } {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => {
    root.render(node);
  });
  return { container, root };
}

function cleanup(root: Root, container: HTMLElement) {
  act(() => {
    root.unmount();
  });
  container.remove();
}

describe('ListRow', () => {
  it('renders the body and a meta row', () => {
    const { container, root } = render(<ListRow body="Hello" meta="today" />);
    expect(container.textContent).toContain('Hello');
    expect(container.textContent).toContain('today');
    cleanup(root, container);
  });

  it('omits the meta row when meta is null', () => {
    const { container, root } = render(<ListRow body="Hello" meta={null} />);
    expect(container.textContent).toContain('Hello');
    expect(container.textContent).not.toContain('today');
    cleanup(root, container);
  });

  it('applies single-line truncate by default and line-clamp-2 when requested', () => {
    const one = render(<ListRow body="A" />);
    expect(one.container.querySelector('.truncate')).not.toBeNull();
    cleanup(one.root, one.container);

    const two = render(<ListRow body="A" bodyLines={2} />);
    expect(two.container.querySelector('.line-clamp-2')).not.toBeNull();
    cleanup(two.root, two.container);
  });

  it('renders the meta leading icon when provided, omits when null', () => {
    const withIcon = render(<ListRow body="A" meta="d" metaIcon={<span data-testid="ic">x</span>} />);
    expect(withIcon.container.querySelector('[data-testid="ic"]')).not.toBeNull();
    cleanup(withIcon.root, withIcon.container);

    const noIcon = render(<ListRow body="A" meta="d" metaIcon={null} />);
    expect(noIcon.container.querySelector('[data-testid="ic"]')).toBeNull();
    cleanup(noIcon.root, noIcon.container);
  });
});

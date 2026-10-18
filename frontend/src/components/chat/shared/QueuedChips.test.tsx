/** QueuedChips — the ✕ on a yellow queued chip takes the warm hover. */
// @vitest-environment jsdom
import { describe, it, expect, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('../../../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

import { QueuedChips } from './QueuedChips';

describe('QueuedChips', () => {
  it('the ✕ hovers warm-strong, not the app-wide gray', () => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    act(() => root.render(createElement(QueuedChips, {
      queued: [{ id: 'm1', text: 'queued', images: 0 }],
      onRemove: () => {},
    })));
    const button = host.querySelector('button[aria-label="removeQueued"]')!;
    expect(button.className).toContain('hover:bg-[var(--warm-strong)]');
    expect(button.className).not.toContain('hover:bg-surface3');
    act(() => root.unmount());
    host.remove();
  });
});

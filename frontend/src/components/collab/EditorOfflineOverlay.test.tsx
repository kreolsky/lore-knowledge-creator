/** Tests for EditorOfflineOverlay — visibility + retry CTA per collab state. */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('../../i18n', () => ({
  useTranslation: () => ({
    t: (key: string, vars?: Record<string, string | number>) => {
      const map: Record<string, string> = {
        collabOverlayConnectingTitle: 'Connecting…',
        collabOverlayReconnectingTitle: 'Reconnecting…',
        collabOverlayReconnectingSub: `Attempt ${vars?.attempt} of ${vars?.max}`,
        collabOverlayOfflineTitle: 'Offline',
        collabOverlayOfflineSub: 'Cannot reach server.',
        collabOverlayRetry: 'Retry',
        collabOverlayMicrocopy: 'On reconnect, the document will update.',
      };
      return map[key] ?? key;
    },
  }),
}));

import { EditorOfflineOverlay } from './EditorOfflineOverlay';

let container: HTMLDivElement;
let root: Root;

function mount(node: React.ReactNode) {
  act(() => { root.render(node); });
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => { root.unmount(); });
  container.remove();
});

const baseProps = {
  reconnectAttempts: 3,
  maxAttempts: 50,
  onRetry: () => {},
};

describe('EditorOfflineOverlay', () => {
  it('is hidden when connected AND collabReady', () => {
    mount(createElement(EditorOfflineOverlay, { ...baseProps, status: 'connected', collabReady: true }));
    expect(container.querySelector('[data-testid="editor-offline-overlay"]')).toBeNull();
  });

  it('is visible when connected but collabReady=false (init pending)', () => {
    mount(createElement(EditorOfflineOverlay, { ...baseProps, status: 'connected', collabReady: false }));
    expect(container.querySelector('[data-testid="editor-offline-overlay"]')).not.toBeNull();
    expect(container.textContent).toContain('Connecting');
  });

  it('shows spinner + attempt counter for reconnecting', () => {
    mount(createElement(EditorOfflineOverlay, { ...baseProps, status: 'reconnecting', collabReady: false }));
    expect(container.querySelector('[data-testid="overlay-spinner"]')).not.toBeNull();
    expect(container.textContent).toContain('Attempt 3 of 50');
  });

  it('shows Retry button when offline and invokes onRetry on click', () => {
    const onRetry = vi.fn();
    mount(createElement(EditorOfflineOverlay, { ...baseProps, status: 'offline', collabReady: false, onRetry }));
    const buttons = Array.from(container.querySelectorAll('button'));
    const retry = buttons.find(b => (b.textContent ?? '').includes('Retry'));
    expect(retry).toBeDefined();
    act(() => { retry!.click(); });
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('hides spinner when offline', () => {
    mount(createElement(EditorOfflineOverlay, { ...baseProps, status: 'offline', collabReady: false }));
    expect(container.querySelector('[data-testid="overlay-spinner"]')).toBeNull();
  });
});

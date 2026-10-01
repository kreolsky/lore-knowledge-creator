/** AdminVersion — the release tag after the admin panel label: `(v…)` once
 * /api/health answers, nothing while loading, a red explicit error on failure. */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, act } from 'react';
import { createRoot, type Root } from 'react-dom/client';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('../i18n', () => ({
  useTranslation: () => ({ t: (k: string) => (k === 'appVersionUnavailable' ? 'Could not load version' : k) }),
}));

import { AdminVersion } from './AdminVersion';

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => { root.unmount(); });
  container.remove();
  vi.unstubAllGlobals();
});

async function renderWith(response: Promise<Response>) {
  vi.stubGlobal('fetch', vi.fn(() => response));
  await act(async () => { root.render(createElement(AdminVersion)); });
}

describe('AdminVersion', () => {
  it('renders the release tag in parentheses once /api/health answers', async () => {
    await renderWith(Promise.resolve(new Response(JSON.stringify({ version: 'v0.21.0' }), { status: 200 })));
    expect(container.textContent).toBe(' (v0.21.0)');
    expect(fetch).toHaveBeenCalledWith('/api/health', { credentials: 'include' });
  });

  it('renders nothing while the read is in flight', async () => {
    await renderWith(new Promise<Response>(() => {}));
    expect(container.textContent).toBe('');
  });

  it('says the version is unavailable, in red, when /api/health fails', async () => {
    await renderWith(Promise.resolve(new Response('', { status: 503 })));
    const span = container.querySelector('span');
    expect(span?.textContent).toBe(' (Could not load version)');
    expect(span?.classList.contains('text-red')).toBe(true);
  });
});

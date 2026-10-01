/**
 * Unit tests for the HistoryPanel snapshot hover preview states (plan
 * snapshot-hover-preview-states): the hover popup must keep error, loading,
 * empty and content bodies distinct, and a late response for a snapshot that
 * is no longer hovered must never be painted.
 *
 * Harness: manual createRoot + act per the repo's component-test pattern
 * (NotesPanel.test.tsx). apiClient.get is mocked with one deferred promise
 * per URL, so each /checkpoints/{id} fetch is stepped by the test; the list
 * and history mount fetches resolve immediately. The REAL
 * HoverPreviewPopup -> LinkPreviewPopup chain renders (portal to
 * document.body) — assertions read the rendered plaque ([data-preview-title])
 * and body texts through the identity t() mock.
 */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// jsdom measures every element as 0x0; useListCapacity would collapse the
// timeline to a single row (computeCapacity returns 1 on a zero row height).
// Give rows and containers a plausible measured height so both snapshot rows
// render at once.
Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get: () => 24 });
Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => 600 });

// ---- store / hook mocks (mutable per test) ----
const appState: Record<string, unknown> = {
  currentDocument: { document_id: 'doc-1', content: '' },
  currentReference: null,
  snapshotPreview: null,
  setSnapshotPreview: vi.fn(),
  openSnapshotModal: vi.fn(),
  showToast: vi.fn(),
};
vi.mock('../store/app-store', () => ({
  useAppStore: Object.assign((sel: (s: unknown) => unknown) => sel(appState), { getState: () => appState }),
}));

const uiState: Record<string, unknown> = { documents: {} };
vi.mock('../store/ui-store', () => ({
  useUIStore: (sel: (s: unknown) => unknown) => sel(uiState),
}));
vi.mock('../store/ui-store/documents-slice', () => ({
  readRefOpenMode: () => null,
  refIsScope: () => false,
}));

vi.mock('../editor/active-editor', () => ({
  useEditorContent: () => () => '',
  getRoleView: () => null,
}));

vi.mock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

// Deferred per URL: /checkpoints/{id} fetches are stepped by the test.
const LIST_URL = '/checkpoints?document_id=doc-1';
const HISTORY_URL = '/documents/doc-1/history';
let listSnapshots: Array<Record<string, unknown>> = [];
const deferreds = new Map<string, { resolve: (v: unknown) => void; reject: (e: unknown) => void }>();
vi.mock('../api/client', () => ({
  apiClient: {
    get: (url: string) => {
      if (url === LIST_URL) return Promise.resolve(listSnapshots);
      if (url === HISTORY_URL) return Promise.resolve([]);
      return new Promise((resolve, reject) => { deferreds.set(url, { resolve, reject }); });
    },
    patch: vi.fn(),
  },
}));

import { HistoryPanel } from './HistoryPanel';

// useHoverPreview's open delay — every hover advances past it.
const HOVER_DELAY = 300;

let root: Root | null = null;
let host: HTMLDivElement | null = null;

function snap(id: string): Record<string, unknown> {
  return {
    checkpoint_id: id,
    document_id: 'doc-1',
    label: '',
    comment: `Comment ${id}`,
    created_at: '2026-01-01T00:00:00Z',
    user_name: 'User',
  };
}

async function mountPanel() {
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root!.render(createElement(HistoryPanel)); });
}

function unmount() {
  // Capture into a local: `root` is a mutable module-level binding, so TS drops
  // the null-narrowing inside the act() closure.
  const r = root;
  if (r) act(() => r.unmount());
  root = null;
  host?.remove();
  host = null;
}

function rowById(id: string): HTMLElement {
  const row = Array.from(host!.querySelectorAll('.doc-item'))
    .find(el => el.textContent?.includes(`Comment ${id}`));
  if (!row) throw new Error(`snapshot row ${id} not rendered`);
  return row as HTMLElement;
}

/** Fire the row onMouseEnter via the mouseover event React synthesizes it from
 * (same pattern as DocumentTree.test.tsx), then cross the open delay. */
function hoverRow(id: string) {
  act(() => { rowById(id).dispatchEvent(new MouseEvent('mouseover', { bubbles: true, relatedTarget: null })); });
  act(() => { vi.advanceTimersByTime(HOVER_DELAY + 50); });
}

/** Move the pointer from one row to another: leaves the first (its mouseleave
 * closes the popup), enters the second, then crosses the open delay again. */
function moveHover(fromId: string, toId: string) {
  const from = rowById(fromId);
  const to = rowById(toId);
  act(() => {
    from.dispatchEvent(new MouseEvent('mouseout', { bubbles: true, relatedTarget: to }));
    to.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, relatedTarget: from }));
  });
  act(() => { vi.advanceTimersByTime(HOVER_DELAY + 50); });
}

/** The rendered popup (portal on document.body). */
function popupText(): string {
  return document.querySelector('div.fixed.z-55')?.textContent ?? '';
}
function plaque(): string | null {
  return document.querySelector('[data-preview-title]')?.textContent ?? null;
}
/** The body wrapper inside the popup (excludes the title plaque). */
function popupBody(): string {
  return document.querySelector('div.fixed.z-55 .p-3')?.textContent ?? '';
}

async function resolveGet(id: string, value: unknown) {
  await act(async () => {
    deferreds.get(`/checkpoints/${id}`)!.resolve(value);
    await Promise.resolve();
    await Promise.resolve();
  });
}
async function rejectGet(id: string) {
  await act(async () => {
    deferreds.get(`/checkpoints/${id}`)!.reject(new Error('network down'));
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  deferreds.clear();
  listSnapshots = [snap('a'), snap('b')];
});

afterEach(() => {
  unmount();
  vi.useRealTimers();
});

describe('HistoryPanel snapshot hover preview states', () => {
  it('failed GET: body is the previewLoadError text under the hovered snapshot plaque, never noContent', async () => {
    await mountPanel();
    hoverRow('a');
    await rejectGet('a');
    expect(plaque()).toBe('Comment a');
    expect(popupText()).toContain('previewLoadError');
    expect(popupText()).not.toContain('noContent');
  });

  it('pending GET: loading body while the plaque already carries the hovered snapshot label', async () => {
    await mountPanel();
    hoverRow('a');
    // The fetch is still in flight (deferred unresolved).
    expect(deferreds.has('/checkpoints/a')).toBe(true);
    expect(plaque()).toBe('Comment a');
    expect(popupText()).toContain('loading');
    expect(popupText()).not.toContain('noContent');
  });

  it('out of order: hover A then B, B resolves first, the late A response is never painted', async () => {
    await mountPanel();
    hoverRow('a');
    moveHover('a', 'b');
    expect(plaque()).toBe('Comment b');
    await resolveGet('b', { ...snap('b'), content: 'Body of b' });
    expect(popupText()).toContain('Body of b');
    await resolveGet('a', { ...snap('a'), content: 'Body of a' });
    expect(plaque()).toBe('Comment b');
    expect(popupText()).toContain('Body of b');
    expect(popupText()).not.toContain('Body of a');
  });

  it('resolved empty content renders noContent — empty is not an error', async () => {
    await mountPanel();
    hoverRow('a');
    await resolveGet('a', { ...snap('a'), content: '' });
    expect(popupText()).toContain('noContent');
    expect(popupText()).not.toContain('previewLoadError');
  });

  it('resolved text renders as the body, capped at 400 chars', async () => {
    await mountPanel();
    hoverRow('a');
    await resolveGet('a', { ...snap('a'), content: 'B'.repeat(450) + 'TAIL' });
    expect(plaque()).toBe('Comment a');
    expect(popupBody().length).toBe(400);
    expect(popupBody().startsWith('B'.repeat(400))).toBe(true);
  });
});

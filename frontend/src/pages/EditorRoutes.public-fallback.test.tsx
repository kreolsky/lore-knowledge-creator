/**
 * EditorRoutes — /docs/:documentId for a SIGNED-IN visitor.
 *
 * Binds the publicFallbackDocIds in ui-store: a published document opens
 * for a logged-in non-member exactly as it does for a logged-out visitor, rather
 * than bouncing them off their own refused read.
 */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MemoryRouter } from 'react-router-dom';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const uiState: Record<string, unknown> = {
  publicFallbackDocIds: new Set<string>(),
  markPublicFallback: vi.fn(),
};
vi.mock('../store/ui-store', () => ({
  useUIStore: Object.assign((sel: (s: unknown) => unknown) => sel(uiState), { getState: () => uiState }),
}));

vi.mock('./ProjectPage', () => ({
  ProjectPage: () => createElement('div', { 'data-testid': 'project-shell' }),
}));
vi.mock('./DocumentPage', () => ({ DocumentPage: () => null }));
vi.mock('./PublicSharePage', () => ({
  PublicSharePage: ({ documentId }: { documentId: string }) =>
    createElement('div', { 'data-testid': 'public-share' }, documentId),
}));

let container: HTMLDivElement;
let root: Root;

async function mountAt(path: string) {
  const { EditorRoutes } = await import('./EditorRoutes');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(createElement(MemoryRouter, { initialEntries: [path] }, createElement(EditorRoutes)));
    await Promise.resolve();
  });
}

function unmount() {
  act(() => root.unmount());
  container.remove();
}

beforeEach(() => {
  vi.clearAllMocks();
  uiState.publicFallbackDocIds = new Set<string>();
});

describe('EditorRoutes — /docs/:id public fallback', () => {
  it('renders the project shell for a document that was never refused', async () => {
    await mountAt('/docs/d-1');
    expect(container.querySelector('[data-testid="project-shell"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="public-share"]')).toBeNull();
    unmount();
  });

  it('renders the published read-only view once this id has been refused', async () => {
    uiState.publicFallbackDocIds = new Set(['d-1']);
    await mountAt('/docs/d-1');
    expect(container.querySelector('[data-testid="public-share"]')?.textContent).toBe('d-1');
    expect(container.querySelector('[data-testid="project-shell"]')).toBeNull();
    unmount();
  });

  it('a refusal on one document does not divert another', async () => {
    uiState.publicFallbackDocIds = new Set(['d-OTHER']);
    await mountAt('/docs/d-1');
    expect(container.querySelector('[data-testid="project-shell"]')).not.toBeNull();
    unmount();
  });
});

/** The reset's clear-sets, read through the real cache owners: a project switch must not hand
 *  one project's transclusion content to the next, a logout must not hand one user's state
 *  to the next, and a reset that beats the editor chunk must not need it. */

// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/** A body fetched after the seed proves the cached one is gone; the seeded one proves it stayed. */
function stubServerBody(body: object): void {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(body), { status: 200 })));
}

async function loadOwners() {
  const [host, effects, widgets, docPreview, refPreview, positions] = await Promise.all([
    import('./editor-host'),
    import('./live-preview/effects'),
    import('./live-preview/widgets'),
    import('../../hooks/useDocumentPreview'),
    import('../../hooks/useReferencePreview'),
    import('../../editor/position-cache'),
  ]);
  effects.transcludeMap.set('e1', { kind: 'doc' } as never);
  widgets.widgetHeightCache.set('w1', 10);
  docPreview.seedDocPreview('d1', 'stale');
  refPreview.seedRefPreview('r1', { content: 'stale' });
  positions.positionCacheSet('d1', { cursor: 1, scroll: 0 });
  return { host, effects, widgets, docPreview, refPreview, positions };
}

describe('resetEditorHost', () => {
  beforeEach(() => {
    vi.resetModules();
    stubServerBody({ content: 'fresh' });
  });
  afterEach(() => vi.unstubAllGlobals());

  // TEST-WHY: pure-logic-edge — a cache that stops registering goes stale with no error anywhere, and no drive can see a module Map.
  it('project scope drops every transclusion source and keeps the per-user position', async () => {
    const o = await loadOwners();
    await expect(o.docPreview.fetchDocumentContent('d1')).resolves.toBe('stale');
    await expect(o.refPreview.fetchRefPreview('r1')).resolves.toMatchObject({ content: 'stale' });

    o.host.resetEditorHost({ scope: 'project' });

    expect(o.effects.transcludeMap.size).toBe(0);
    expect(o.widgets.widgetHeightCache.size).toBe(0);
    await expect(o.docPreview.fetchDocumentContent('d1')).resolves.toBe('fresh');
    await expect(o.refPreview.fetchRefPreview('r1')).resolves.toMatchObject({ content: 'fresh' });
    expect(o.positions.positionCacheGet('d1')).toBeDefined();
  });

  // TEST-WHY: pure-logic-edge — the two scopes are different clear-sets by design, and unifying them is a cross-project leak or a behaviour change.
  it('user scope drops the position and keeps the transclusion sources', async () => {
    const o = await loadOwners();

    o.host.resetEditorHost({ scope: 'user' });

    expect(o.positions.positionCacheGet('d1')).toBeUndefined();
    expect(o.effects.transcludeMap.size).toBe(1);
    await expect(o.docPreview.fetchDocumentContent('d1')).resolves.toBe('stale');
  });

  // TEST-WHY: pure-logic-edge — the reset runs on /projects with the editor chunk never loaded, and an owner that loads afterwards must still be covered.
  it('a reset before any owner loaded is a no-op, and an owner loaded after it is still cleared', async () => {
    const { resetEditorHost } = await import('./editor-host');
    expect(() => resetEditorHost({ scope: 'project' })).not.toThrow();

    const { transcludeMap } = await import('./live-preview/effects');
    transcludeMap.set('e1', { kind: 'doc' } as never);
    resetEditorHost({ scope: 'project' });

    expect(transcludeMap.size).toBe(0);
  });
});

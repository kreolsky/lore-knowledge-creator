/**
 * Tests for editor-host — resetEditorHost scope isolation.
 *
 * Verifies the behaviour-preserving invariants of resetEditorHost:
 *   1. Each reset scope clears ONLY its own set (no cross-contamination).
 *   2. Both preview caches are project-scope clears; the user scope refuses them.
 */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { resetEditorHost } from './editor-host';
import * as effectsNS from './live-preview/effects';
import * as widgetsNS from './live-preview/widgets';
import * as docPreviewNS from '../../hooks/useDocumentPreview';
import * as contentSyncNS from '../../editor/content-sync';
import * as positionCacheNS from '../../editor/position-cache';
import * as uiStoreNS from '../../store/ui-store';
import * as refPreviewNS from '../../hooks/useReferencePreview';

// Project-scope set
const PROJECT_CLEARS = [
  ['clearTranscludeMap', effectsNS],
  ['clearWidgetHeightCache', widgetsNS],
  ['clearPreviewCache', docPreviewNS],
  ['clearRefPreviewCache', refPreviewNS],
] as const;

// User-scope set
const USER_CLEARS = [
  ['clearAllCheckpointDedup', contentSyncNS],
  ['clearPositionCache', positionCacheNS],
  ['clearLastSavedBlobs', uiStoreNS],
] as const;

function namesOf(calls: { name: string }[]) {
  return calls.map((c) => c.name).sort();
}

describe('resetEditorHost — scope isolation', () => {
  let spies: Record<string, ReturnType<typeof vi.spyOn>>;

  beforeEach(() => {
    spies = {};
    for (const [name, ns] of [...PROJECT_CLEARS, ...USER_CLEARS]) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      spies[name] = vi.spyOn(ns as Record<string, any>, name).mockImplementation(() => undefined);
    }
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("scope:'project' clears ONLY transcludeMap + widgetHeight + preview", () => {
    resetEditorHost({ scope: 'project' });

    const calledNames = Object.entries(spies)
      .filter(([, s]) => s.mock.calls.length > 0)
      .map(([name]) => name);

    expect(calledNames.sort()).toEqual(
      ['clearPreviewCache', 'clearRefPreviewCache', 'clearTranscludeMap', 'clearWidgetHeightCache'],
    );
    // User-scope clears must NOT fire.
    expect(spies.clearAllCheckpointDedup).not.toHaveBeenCalled();
    expect(spies.clearPositionCache).not.toHaveBeenCalled();
    expect(spies.clearLastSavedBlobs).not.toHaveBeenCalled();
  });

  it("scope:'user' clears ONLY checkpointDedup + positionCache + lastSavedBlobs", () => {
    resetEditorHost({ scope: 'user' });

    const calledNames = Object.entries(spies)
      .filter(([, s]) => s.mock.calls.length > 0)
      .map(([name]) => name);

    expect(calledNames.sort()).toEqual(
      ['clearAllCheckpointDedup', 'clearLastSavedBlobs', 'clearPositionCache'],
    );
    // Project-scope clears must NOT fire.
    expect(spies.clearTranscludeMap).not.toHaveBeenCalled();
    expect(spies.clearWidgetHeightCache).not.toHaveBeenCalled();
    expect(spies.clearPreviewCache).not.toHaveBeenCalled();
    // The reference body cache is dropped on logout by the logout registry
    // (useReferencePreview self-registers), never by this scope.
    expect(spies.clearRefPreviewCache).not.toHaveBeenCalled();
  });

});

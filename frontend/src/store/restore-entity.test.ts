/**
 * resolveRestoredReference — the persisted per-doc reference pointer vs a fetched
 * references list, per the reference open mode.
 *
 * Panel quick preview contract: a cross-doc reference previewed in this document
 * is NOT in the doc's ancestor-scoped list. On reload the saved id must survive
 * as an id-only stub (setCurrentDocument's post-commit hydrate fills the body),
 * NOT be cleared as stale. In the scope modes ('center'/'split') a saved id that
 * the list no longer carries is stale → cleared + null, as before.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { useUIStore, registerAppBridge, clearLastSavedBlobs } from './ui-store';
import { refIsScope } from './ui-store/documents-slice';
import { resolveRestoredReference } from './restore-entity';
import type { Reference } from '../types';

interface AppCtx {
  currentUser: { user_id: string } | null;
  currentProject: { project_id: string } | null;
  currentDocument: { document_id: string } | null;
}

let _ctx: AppCtx = { currentUser: null, currentProject: null, currentDocument: null };

const NOW = '2026-01-01T00:00:00Z';
const makeRef = (id: string, document_id: string | null): Reference => ({
  reference_id: id, project_id: 'p1', document_id, title: id, media_type: 'markdown',
  source_url: null, content: '', processing_status: null, file_path: null, file_meta: null,
  created_at: NOW, updated_at: NOW,
});

beforeEach(() => {
  _ctx = {
    currentUser: { user_id: 'u1' },
    currentProject: { project_id: 'p1' },
    currentDocument: null,
  };
  registerAppBridge({
    getAppContext: () => _ctx,
    showToast: () => {},
  });
  clearLastSavedBlobs();
  // projectPrefsLoaded gates triggerSaveUI — emulate the hydrated runtime so
  // pointer writes don't queue un-awaited persistence timers.
  useUIStore.setState({ documents: {}, projectPrefsLoaded: true });
  vi.spyOn(globalThis, 'fetch').mockResolvedValue({
    ok: true, status: 200, json: () => Promise.resolve({}),
  } as Response);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('resolveRestoredReference — in-list pointer', () => {
  it('returns the list ref when the saved id is present (any mode)', () => {
    useUIStore.getState().setRefOpenMode('doc-1', 'panel');
    useUIStore.getState().setCurrentReferenceForDoc('doc-1', 'ref-in');
    const r = resolveRestoredReference('doc-1', [makeRef('ref-in', 'doc-1')]);
    expect(r?.reference_id).toBe('ref-in');
    expect(r?.title).toBe('ref-in');
  });

  it('returns null when nothing was persisted', () => {
    expect(resolveRestoredReference('doc-1', [makeRef('ref-in', 'doc-1')])).toBeNull();
  });
});

describe('resolveRestoredReference — absent pointer (panel quick preview)', () => {
  it('panel: saved ref absent from the list → id-only stub, saved id NOT cleared', () => {
    useUIStore.getState().setRefOpenMode('doc-1', 'panel');
    useUIStore.getState().setCurrentReferenceForDoc('doc-1', 'ref-foreign');
    const r = resolveRestoredReference('doc-1', [makeRef('ref-in', 'doc-1')]);
    // Id-only stub: the post-commit hydrate fills the body from the id.
    expect(r).toEqual({ reference_id: 'ref-foreign' });
    expect(useUIStore.getState().getCurrentReferenceForDoc('doc-1')).toBe('ref-foreign');
  });

  it('center: saved ref absent from the list → null and the stale id cleared (legacy behavior)', () => {
    useUIStore.getState().setCurrentReferenceForDoc('doc-1', 'ref-foreign');
    expect(resolveRestoredReference('doc-1', [])).toBeNull();
    expect(useUIStore.getState().getCurrentReferenceForDoc('doc-1')).toBeNull();
  });

  it('split: absent ref also clears (split is a scope mode)', () => {
    useUIStore.getState().setRefOpenMode('doc-1', 'split');
    useUIStore.getState().setCurrentReferenceForDoc('doc-1', 'ref-foreign');
    expect(resolveRestoredReference('doc-1', [])).toBeNull();
    expect(useUIStore.getState().getCurrentReferenceForDoc('doc-1')).toBeNull();
  });
});

describe('refIsScope — the ONE projection (panel quick preview)', () => {
  it('panel is not a scope mode; center and split are', () => {
    expect(refIsScope('panel')).toBe(false);
    expect(refIsScope('center')).toBe(true);
    expect(refIsScope('split')).toBe(true);
  });
});

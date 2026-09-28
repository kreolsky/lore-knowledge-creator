/** Unit tests for content-sync — pure-logic functions + checkpointContent doors.
 *
 * syncToStore's setState side effects are integration territory — out of unit scope.
 * Manual testing required for store writes.
 *
 * persist is flushAndWait-only (the CRDT is the editor's single write door): these
 * tests pin the flush path, its failure toast (no REST fallback), and the dedup cache.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('../i18n', () => ({
  t: (key: string) => key,
  useTranslation: () => ({ t: (key: string) => key }),
}));

import {
  readEditorContent,
  mergeContentIntoDoc,
  checkpointContent,
  clearAllCheckpointDedup,
  type PersistOptions,
} from './content-sync';
import { apiClient } from '../api/client';
import { useAppStore } from '../store/app-store';
import type { Document } from '../types';

// ── Helpers ──────────────────────────────────────────────────────────────────

function makeDoc(overrides: Partial<Document> = {}): Document {
  return {
    document_id: 'doc-1',
    project_id: 'proj-1',
    parent_id: null,
    title: 'Test Doc',
    content: 'old content',
    path: 'test.md',
    is_index: false,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  };
}

function mockEditorViewRef(text: string | null) {
  if (text === null) {
    return { current: null };
  }
  return {
    current: {
      state: {
        doc: {
          toString: () => text,
        },
      },
    },
  };
}

/** Fake collab provider: only the flushAndWait surface persist touches. */
function makeProvider() {
  return {
    flushAndWait: vi.fn().mockResolvedValue(undefined),
    send: vi.fn(),
  };
}

function connectedOptions(provider: ReturnType<typeof makeProvider>): PersistOptions {
  return {
    collabStatus: 'connected',
    projectCollab: provider as unknown as PersistOptions['projectCollab'],
  };
}

// ── readEditorContent ────────────────────────────────────────────────────────

describe('readEditorContent', () => {
  it('returns doc content when view is present', () => {
    const ref = mockEditorViewRef('hello world');
    expect(readEditorContent(ref as any)).toBe('hello world');
  });

  it('returns null when view is null', () => {
    const ref = mockEditorViewRef(null);
    expect(readEditorContent(ref as any)).toBeNull();
  });

  it('returns empty string for empty doc', () => {
    const ref = mockEditorViewRef('');
    expect(readEditorContent(ref as any)).toBe('');
  });

  it('returns multi-line content preserving newlines', () => {
    const content = 'line1\nline2\nline3';
    const ref = mockEditorViewRef(content);
    expect(readEditorContent(ref as any)).toBe(content);
  });
});

// ── mergeContentIntoDoc ──────────────────────────────────────────────────────

describe('mergeContentIntoDoc', () => {
  it('returns doc with updated content when id matches', () => {
    const doc = makeDoc();
    const result = mergeContentIntoDoc(doc, 'doc-1', 'new content');
    expect(result.content).toBe('new content');
    expect(result.document_id).toBe('doc-1');
  });

  it('returns unchanged doc when id does not match', () => {
    const doc = makeDoc();
    const result = mergeContentIntoDoc(doc, 'doc-other', 'new content');
    expect(result.content).toBe('old content');
    expect(result).toEqual(doc);
  });

  it('preserves other fields', () => {
    const doc = makeDoc({ title: 'My Title', headings: [] });
    const result = mergeContentIntoDoc(doc, 'doc-1', 'x');
    expect(result.title).toBe('My Title');
    expect(result.headings).toEqual([]);
  });

  it('does not mutate the original', () => {
    const doc = makeDoc();
    const result = mergeContentIntoDoc(doc, 'doc-1', 'new');
    expect(doc.content).toBe('old content');
    expect(result.content).toBe('new');
  });
});

// ── checkpointContent empty-read guard (empty-checkpoint wipe) ───────────────

describe('checkpointContent empty-read guard', () => {
  beforeEach(() => {
    clearAllCheckpointDedup();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('empty view → no flush, warn logged, promise resolves', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const provider = makeProvider();

    const doc = makeDoc({ document_id: 'doc-empty' });
    const ref = mockEditorViewRef('') as any;

    await expect(checkpointContent(ref, doc, connectedOptions(provider))).resolves.toBeUndefined();
    expect(provider.flushAndWait).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalled();
  });

  it('whitespace-only view → same skip', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const provider = makeProvider();

    const doc = makeDoc({ document_id: 'doc-ws' });
    const ref = mockEditorViewRef("  \n\t ") as any;

    await expect(checkpointContent(ref, doc, connectedOptions(provider))).resolves.toBeUndefined();
    expect(provider.flushAndWait).not.toHaveBeenCalled();
  });

  it('skip does not pollute the dedup cache — a later non-empty checkpoint flushes', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const provider = makeProvider();

    const doc = makeDoc({ document_id: 'doc-mixed' });
    const emptyRef = mockEditorViewRef('') as any;
    const fullRef = mockEditorViewRef('real content') as any;

    await checkpointContent(emptyRef, doc, connectedOptions(provider));
    expect(provider.flushAndWait).not.toHaveBeenCalled();

    await checkpointContent(fullRef, doc, connectedOptions(provider));
    expect(provider.flushAndWait).toHaveBeenCalledTimes(1);
  });

  it('non-empty content → flush still fires (guard does not over-fire)', async () => {
    const provider = makeProvider();

    const doc = makeDoc({ document_id: 'doc-nonempty' });
    const ref = mockEditorViewRef('content-x') as any;

    await checkpointContent(ref, doc, connectedOptions(provider));
    expect(provider.flushAndWait).toHaveBeenCalledTimes(1);
  });
});

// ── checkpointContent dedup cache + the flush-only write door ────────────────

describe('checkpointContent dedup cache', () => {
  beforeEach(() => {
    clearAllCheckpointDedup();
    useAppStore.setState({ documents: [] });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    useAppStore.setState({ documents: [] });
  });

  it('successful flush updates cache — second call skips the flush', async () => {
    const provider = makeProvider();

    const doc = makeDoc({ document_id: 'doc-cache', content: '' });
    const ref = mockEditorViewRef('content-a') as any;

    await checkpointContent(ref, doc, connectedOptions(provider));
    expect(provider.flushAndWait).toHaveBeenCalledTimes(1);

    await checkpointContent(ref, doc, connectedOptions(provider));
    expect(provider.flushAndWait).toHaveBeenCalledTimes(1);
  });

  it('failed flush does NOT update cache — retry allowed on same content', async () => {
    const provider = makeProvider();
    provider.flushAndWait.mockRejectedValueOnce(new Error('flush down'));

    const doc = makeDoc({ document_id: 'doc-cache2', content: '' });
    const ref = mockEditorViewRef('content-b') as any;

    await expect(checkpointContent(ref, doc, connectedOptions(provider))).rejects.toThrow('flush down');

    await checkpointContent(ref, doc, connectedOptions(provider));
    expect(provider.flushAndWait).toHaveBeenCalledTimes(2);
  });
});

describe('persist — flushAndWait is the only write door', () => {
  let patchSpy: ReturnType<typeof vi.spyOn>;
  let toastSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    clearAllCheckpointDedup();
    patchSpy = vi.spyOn(apiClient, 'patch').mockResolvedValue(undefined as never);
    toastSpy = vi.spyOn(useAppStore.getState(), 'showToast').mockImplementation(() => {});
    useAppStore.setState({ documents: [] });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    useAppStore.setState({ documents: [] });
  });

  it('failing flushAndWait → failedToSaveDocument toast, no REST PATCH', async () => {
    // Seed the store so a resurrected REST fallback WOULD pass its guards and PATCH.
    const doc = makeDoc({ document_id: 'doc-fallback' });
    useAppStore.setState({ documents: [doc] });
    const provider = makeProvider();
    provider.flushAndWait.mockRejectedValue(new Error('flush down'));

    const ref = mockEditorViewRef('content-f') as any;

    await expect(checkpointContent(ref, doc, connectedOptions(provider))).rejects.toThrow('flush down');
    expect(toastSpy).toHaveBeenCalledWith('failedToSaveDocument', 'error');
    expect(patchSpy).not.toHaveBeenCalled();
  });

  it('collab not connected → no flush, no PATCH, resolves', async () => {
    const provider = makeProvider();
    const doc = makeDoc({ document_id: 'doc-offline' });
    const ref = mockEditorViewRef('content-o') as any;

    await expect(checkpointContent(ref, doc, {
      collabStatus: undefined,
      projectCollab: provider as unknown as PersistOptions['projectCollab'],
    })).resolves.toBeUndefined();
    expect(provider.flushAndWait).not.toHaveBeenCalled();
    expect(patchSpy).not.toHaveBeenCalled();
  });
});

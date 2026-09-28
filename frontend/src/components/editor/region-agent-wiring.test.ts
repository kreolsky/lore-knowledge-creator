// @vitest-environment jsdom
/**
 * Tests for region-agent-wiring — the CM6 half of the pinned-region highlight:
 * makeRegionResolver (the facet resolver over chat-store + pending-selection) and
 * regionLostListener (the docChanged auto-unpin). Editor.tsx mounts both; these
 * suites pin the branch table without mounting the component.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { EditorView } from '@codemirror/view';
import { EditorState } from '@codemirror/state';

interface SessionStub {
  session_id: string;
  has_region?: boolean;
  target_doc_id?: string | null;
  document_id?: string;
}

// Mutable stand-in the wiring reads through useChatStore.getState().
const storeState = {
  activeSessionId: null as string | null,
  ghostRegion: null as { doc_id: string } | null,
  sessions: [] as SessionStub[],
  clearGhostRegion: vi.fn(),
  regionLost: vi.fn(),
};

vi.mock('../../store/chat-store', () => ({
  useChatStore: { getState: () => storeState },
}));
vi.mock('../../store/chat-store/pending-selection', () => ({
  getPendingRegion: vi.fn(() => null),
  resolveRegion: vi.fn(() => ({ status: 'ok' })),
  resolveRegionRange: vi.fn(() => null),
}));

import {
  makeRegionResolver,
  regionLostListener,
} from './region-agent-wiring';
import {
  getPendingRegion,
  resolveRegion,
  resolveRegionRange,
} from '../../store/chat-store/pending-selection';

const RANGE = { from: 3, to: 9 };

beforeEach(() => {
  vi.clearAllMocks();
  storeState.activeSessionId = null;
  storeState.ghostRegion = null;
  storeState.sessions = [];
  vi.mocked(resolveRegionRange).mockReturnValue(null);
});

describe('makeRegionResolver', () => {
  const resolverFor = (myDocId: string | undefined) => makeRegionResolver(() => myDocId);

  it('resolves a ghost pin on the matching doc', () => {
    storeState.activeSessionId = null;
    storeState.ghostRegion = { doc_id: 'doc-1' };
    vi.mocked(resolveRegionRange).mockReturnValue(RANGE);
    expect(resolverFor('doc-1')()).toBe(RANGE);
    expect(resolveRegionRange).toHaveBeenCalledWith(storeState.ghostRegion);
  });

  it('returns null for a ghost pin on a different doc', () => {
    storeState.ghostRegion = { doc_id: 'doc-other' };
    expect(resolverFor('doc-1')()).toBeNull();
    expect(resolveRegionRange).not.toHaveBeenCalled();
  });

  it('returns null with no ghost and no session', () => {
    expect(resolverFor('doc-1')()).toBeNull();
  });

  it('returns null for an active session without a region', () => {
    storeState.activeSessionId = 's1';
    storeState.sessions = [{ session_id: 's1', has_region: false }];
    expect(resolverFor('doc-1')()).toBeNull();
  });

  it('returns null when the session region targets a different doc', () => {
    storeState.activeSessionId = 's1';
    storeState.sessions = [{ session_id: 's1', has_region: true, document_id: 'doc-other' }];
    expect(resolverFor('doc-1')()).toBeNull();
  });

  it('resolves a materialized region on the matching doc', () => {
    storeState.activeSessionId = 's1';
    storeState.sessions = [{ session_id: 's1', has_region: true, document_id: 'doc-1' }];
    const region = { doc_id: 'doc-1' };
    vi.mocked(getPendingRegion).mockReturnValue(region as never);
    vi.mocked(resolveRegionRange).mockReturnValue(RANGE);
    expect(resolverFor('doc-1')()).toBe(RANGE);
    expect(getPendingRegion).toHaveBeenCalledWith('s1');
  });

  it('prefers target_doc_id over document_id', () => {
    storeState.activeSessionId = 's1';
    storeState.sessions = [{ session_id: 's1', has_region: true, target_doc_id: 'doc-1', document_id: 'doc-other' }];
    vi.mocked(getPendingRegion).mockReturnValue({ doc_id: 'doc-1' } as never);
    vi.mocked(resolveRegionRange).mockReturnValue(RANGE);
    expect(resolverFor('doc-1')()).toBe(RANGE);
  });

  it('returns null when the pending region is missing', () => {
    storeState.activeSessionId = 's1';
    storeState.sessions = [{ session_id: 's1', has_region: true, document_id: 'doc-1' }];
    vi.mocked(getPendingRegion).mockReturnValue(null);
    expect(resolverFor('doc-1')()).toBeNull();
  });
});

describe('regionLostListener', () => {
  function makeView(myDocId = 'doc-1'): EditorView {
    return new EditorView({
      state: EditorState.create({
        doc: 'hello',
        extensions: [regionLostListener(() => myDocId)],
      }),
      parent: document.createElement('div'),
    });
  }

  it('clears a ghost pin whose anchor died on a docChanged transaction', () => {
    storeState.activeSessionId = null;
    storeState.ghostRegion = { doc_id: 'doc-1' };
    vi.mocked(resolveRegionRange).mockReturnValue(null);
    const view = makeView();
    view.dispatch({ changes: { from: 0, insert: 'x' } });
    expect(storeState.clearGhostRegion).toHaveBeenCalledTimes(1);
    view.destroy();
  });

  it('keeps a ghost pin whose anchor still resolves', () => {
    storeState.activeSessionId = null;
    storeState.ghostRegion = { doc_id: 'doc-1' };
    vi.mocked(resolveRegionRange).mockReturnValue(RANGE);
    const view = makeView();
    view.dispatch({ changes: { from: 0, insert: 'x' } });
    expect(storeState.clearGhostRegion).not.toHaveBeenCalled();
    view.destroy();
  });

  it('unpins a session region reported lost', () => {
    storeState.activeSessionId = 's1';
    storeState.sessions = [{ session_id: 's1', has_region: true, document_id: 'doc-1' }];
    vi.mocked(resolveRegion).mockReturnValue({ status: 'lost' });
    const view = makeView();
    view.dispatch({ changes: { from: 0, insert: 'x' } });
    expect(storeState.regionLost).toHaveBeenCalledWith('s1');
    view.destroy();
  });

  it('does nothing on non-docChanged transactions', () => {
    storeState.activeSessionId = 's1';
    storeState.sessions = [{ session_id: 's1', has_region: true, document_id: 'doc-1' }];
    vi.mocked(resolveRegion).mockReturnValue({ status: 'lost' });
    const view = makeView();
    view.dispatch({ selection: { anchor: 2 } });
    expect(storeState.regionLost).not.toHaveBeenCalled();
    view.destroy();
  });

  it('ignores a session region targeting a different doc', () => {
    storeState.activeSessionId = 's1';
    storeState.sessions = [{ session_id: 's1', has_region: true, document_id: 'doc-other' }];
    vi.mocked(resolveRegion).mockReturnValue({ status: 'lost' });
    const view = makeView('doc-1');
    view.dispatch({ changes: { from: 0, insert: 'x' } });
    expect(storeState.regionLost).not.toHaveBeenCalled();
    view.destroy();
  });
});

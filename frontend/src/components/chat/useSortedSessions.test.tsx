/** useSortedSessions — recency DESC (last user msg ?? created_at) OR per-doc proximity sort.
 * When chatSortByProximity is ON for the open
 * document, sessions rank by tree distance to that anchor, tiebreak last-user-msg DESC.
 * Recency fallback is created_at (immutable) — NEVER updated_at (which auto_title /
 * update_session bump and would reorder the list on enter→leave). */
// @vitest-environment jsdom

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// Module-level state the mocked stores read from; each test sets its own.
let chatState: { sessions: unknown[] } = { sessions: [] };
// Default: no open doc, proximity OFF. Tests override getChatSortByProximity.
let uiState: { getChatSortByProximity: (docId: string | null) => boolean } = {
  getChatSortByProximity: () => false,
};
let appState: { currentDocument: { document_id: string } | null; documents: unknown[] } = {
  currentDocument: null,
  documents: [],
};

vi.mock('../../store/chat-store', () => ({
  useChatStore: (selector: (s: unknown) => unknown) => selector(chatState),
}));
vi.mock('../../store/ui-store', () => ({
  useUIStore: (selector: (s: unknown) => unknown) => selector(uiState),
}));
vi.mock('../../store/app-store', () => ({
  useAppStore: (selector: (s: unknown) => unknown) => selector(appState),
}));

import { useSortedSessions } from './useSortedSessions';
import type { ChatSession } from '../../types';

// lm = last_message_at, created = created_at. updated_at is set but must NOT affect
// the comparator — created_at is the immutable fallback.
function session(
  id: string,
  lm: string | null,
  created: string,
  title = `t-${id}`,
  documentId: string | null = null,
): ChatSession {
  return {
    session_id: id,
    document_id: documentId,
    reference_id: null,
    user_id: 'u1',
    title,
    model: 'm',
    system_prompt_id: null,
    context_ids: [],
    // updated_at deliberately diverges from created_at where a test needs to prove
    // it is ignored by the recency comparator.
    updated_at: created,
    created_at: created,
    last_message_at: lm,
  } as unknown as ChatSession;
}

/** Render the hook via a probe component; returns the comma-joined sorted ids. */
function renderHooked(): { text: string; root: Root } {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  let text = '';
  function Probe(): null {
    const sorted = useSortedSessions();
    text = sorted.map(s => (s as ChatSession).session_id).join(',');
    return null;
  }
  act(() => {
    root.render(createElement(Probe));
  });
  return { text, root };
}

describe('useSortedSessions — recency (last user msg ?? created_at)', () => {
  beforeEach(() => {
    uiState = { getChatSortByProximity: () => false };
    appState = { currentDocument: null, documents: [] };
  });

  it('sorts all sessions by last_message_at ?? created_at DESC', () => {
    // Input deliberately UNSORTED so the test proves the hook reorders.
    chatState = {
      sessions: [
        session('B', null, '2026-02-01T00:00:00Z'),
        session('C', '2026-01-01T00:00:00Z', '2026-04-01T00:00:00Z'),
        session('A', '2026-03-01T00:00:00Z', '2026-01-01T00:00:00Z'),
      ],
    };
    const { text, root } = renderHooked();
    expect(text).toBe('A,B,C');
    act(() => root.unmount());
  });

  it('ignores updated_at — falls back to created_at, not updated_at (F3)', () => {
    // X is the NEWEST by created_at (May) but the OLDEST by updated_at (Jan);
    // Y is the opposite. A comparator using updated_at would rank Y first.
    chatState = {
      sessions: [
        session('X', null, '2026-05-01T00:00:00Z'),
        session('Y', null, '2026-03-01T00:00:00Z'),
      ],
    };
    // Force updated_at to diverge from created_at so the test is meaningful.
    (chatState.sessions[0] as ChatSession).updated_at = '2026-01-01T00:00:00Z'; // X old updated
    (chatState.sessions[1] as ChatSession).updated_at = '2026-09-01T00:00:00Z'; // Y future updated
    const { text, root } = renderHooked();
    expect(text).toBe('X,Y'); // created_at wins → X(May) before Y(Mar)
    act(() => root.unmount());
  });

  it('promotes a freshly-bumped session (live send) to the top', () => {
    chatState = {
      sessions: [
        session('old', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
        session('fresh', '2026-07-06T17:00:00Z', '2026-01-01T00:00:00Z'),
        session('mid', '2026-04-01T00:00:00Z', '2026-04-01T00:00:00Z'),
      ],
    };
    const { text, root } = renderHooked();
    expect(text).toBe('fresh,mid,old');
    act(() => root.unmount());
  });

  it('includes untitled sessions (no title filter)', () => {
    chatState = {
      sessions: [
        session('titled', '2026-03-01T00:00:00Z', '2026-03-01T00:00:00Z', 'Has Title'),
        session('untitled', '2026-05-01T00:00:00Z', '2026-05-01T00:00:00Z', ''),
      ],
    };
    const { text, root } = renderHooked();
    // Untitled participates in the same time sort — never filtered out.
    expect(text).toBe('untitled,titled');
    act(() => root.unmount());
  });
});

describe('useSortedSessions — per-document proximity sort (F2)', () => {
  // Tree:  root D (parent null), D→C1, D→C2 ; sibling root S (parent null)
  // anchor = D. Distances: D=0, C1/C2=1, S=2 (via project root).
  const docs = [
    { document_id: 'D', parent_id: null, title: 'D' },
    { document_id: 'C1', parent_id: 'D', title: 'C1' },
    { document_id: 'C2', parent_id: 'D', title: 'C2' },
    { document_id: 'S', parent_id: null, title: 'S' },
  ];

  beforeEach(() => {
    appState = { currentDocument: { document_id: 'D' }, documents: docs };
    uiState = { getChatSortByProximity: () => true };
  });

  it('ranks own-doc session above a sibling-doc session regardless of recency', () => {
    // sess-C is on D (distance 0) but OLDEST; sess-B is on S (distance 2) but NEWEST.
    chatState = {
      sessions: [
        session('sessA', null, '2026-01-01T00:00:00Z', 'A', 'C1'), // dist 1
        session('sessB', '2026-03-01T00:00:00Z', '2026-03-01T00:00:00Z', 'B', 'S'), // dist 2, newest
        session('sessC', '2026-02-01T00:00:00Z', '2026-02-01T00:00:00Z', 'C', 'D'), // dist 0, mid
      ],
    };
    const { text, root } = renderHooked();
    // Proximity order: C(0), A(1), B(2) — NOT recency (which would be B, C, A).
    expect(text).toBe('sessC,sessA,sessB');
    act(() => root.unmount());
  });

  it('tiebreaks equal-distance sessions by last-user-msg DESC (recency within tier)', () => {
    // Both on C1/C2 (distance 1) — newer user msg ranks first within the tier.
    chatState = {
      sessions: [
        session('olderTier', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'O', 'C1'),
        session('newerTier', '2026-06-01T00:00:00Z', '2026-06-01T00:00:00Z', 'N', 'C2'),
      ],
    };
    const { text, root } = renderHooked();
    expect(text).toBe('newerTier,olderTier');
    act(() => root.unmount());
  });

  it('a doc-less session ranks via project root (finite distance), not crashed', () => {
    chatState = {
      sessions: [
        session('noDoc', '2026-05-01T00:00:00Z', '2026-05-01T00:00:00Z', 'N', null), // doc_id null
        session('onS', '2026-03-01T00:00:00Z', '2026-03-01T00:00:00Z', 'S', 'S'), // dist 2
      ],
    };
    const { text, root } = renderHooked();
    // noDoc → project root: distance D→root(1)+root→(null treated as root). onS dist 2.
    // Either order is acceptable per plan, but it must NOT throw and must return both.
    expect(text.split(',').sort()).toEqual(['noDoc', 'onS']);
    act(() => root.unmount());
  });
});

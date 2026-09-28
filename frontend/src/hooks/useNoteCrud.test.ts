/**
 * TDD: split-mode note-set predicate.
 *
 * In split view BOTH the document (left) and the open reference (right) are
 * visible, so the merged notes list must show:
 *   - all document notes (reference_id === null)
 *   - notes attached to the OPEN reference (reference_id === openRefId)
 * and EXCLUDE notes attached to any OTHER reference.
 *
 * Pure predicate extracted from useNoteCrud.noteItems for testability (the hook
 * memo is covered by manual testing per testing.md — UI/rendering).
 */
import { describe, it, expect } from 'vitest';
import type { ChatSession } from '../types';
import { selectSplitNoteItems } from './useNoteCrud';

function makeSession(
  id: string,
  reference_id: string | null,
  updated_at = '2026-01-01T00:00:00Z',
): ChatSession {
  return {
    session_id: id,
    project_id: 'p',
    document_id: 'd',
    reference_id,
    user_id: 'u',
    title: '',
    model: 'm',
    system_prompt_id: null,
    context_ids: [],
    is_note: true,
    created_at: updated_at,
    updated_at,
  };
}

describe('selectSplitNoteItems — split-mode note set', () => {
  it('includes document notes (reference_id null) and open-reference notes', () => {
    const items = [
      makeSession('doc-anchored', null),
      makeSession('doc-anchorless', null),
      makeSession('ref-note', 'ref-A'),
    ];
    expect(selectSplitNoteItems(items, 'ref-A').map(s => s.session_id))
      .toEqual(['doc-anchored', 'doc-anchorless', 'ref-note']);
  });

  it('excludes notes attached to a DIFFERENT (not open) reference', () => {
    const items = [
      makeSession('doc-note', null),
      makeSession('open-ref-note', 'ref-A'),
      makeSession('other-ref-note', 'ref-B'),
    ];
    expect(selectSplitNoteItems(items, 'ref-A').map(s => s.session_id))
      .toEqual(['doc-note', 'open-ref-note']);
  });

  it('does not mutate the input array order beyond filtering (recency sort handled upstream)', () => {
    const items = [
      makeSession('a', 'ref-A'),
      makeSession('b', null),
      makeSession('c', 'ref-B'),
      makeSession('d', 'ref-A'),
    ];
    expect(selectSplitNoteItems(items, 'ref-A').map(s => s.session_id))
      .toEqual(['a', 'b', 'd']);
  });

  it('returns all doc notes even when no reference is open is NOT this fn concern', () => {
    // Sanity: with only doc notes present, all survive regardless of openRefId.
    const items = [makeSession('only-doc', null)];
    expect(selectSplitNoteItems(items, 'ref-A').map(s => s.session_id))
      .toEqual(['only-doc']);
  });
});

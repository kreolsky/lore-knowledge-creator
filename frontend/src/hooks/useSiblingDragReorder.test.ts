/** useSiblingDragReorder — tree adapter pin.
 *
 * The tree adapter is the old useTreeDragReorder code moved behind the adapter
 * seam; this file pins that the move is behaviour-identical: for a fixed
 * sibling fixture, orderedSiblings returns the same persisted order (and thus
 * the drop computes the same after_id) as before the refactor.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach } from 'vitest';
import { useAppStore } from '../store/app-store';
import { treeDragAdapter, dropAfterId } from './useSiblingDragReorder';
import type { Document } from '../types';

function makeDoc(overrides: Partial<Document> = {}): Document {
  return {
    document_id: 'doc-1',
    project_id: 'proj-1',
    parent_id: 'p',
    title: 'Doc',
    content: '',
    path: 'doc.md',
    is_index: false,
    sort_key: 'a0',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  // Fixture: parent 'p' holds self, an unkeyed row, keyed rows in scrambled
  // store order, and the project index (never reorderable); 'q' is a sibling
  // LEVEL, not a member of the group.
  useAppStore.getState().setDocuments([
    makeDoc({ document_id: 'idx', parent_id: 'p', is_index: true, sort_key: 'a9' }),
    makeDoc({ document_id: 'self', parent_id: 'p', sort_key: 'a3' }),
    makeDoc({ document_id: 'beta', parent_id: 'p', sort_key: 'a5' }),
    makeDoc({ document_id: 'gamma', parent_id: 'p', sort_key: undefined }),
    makeDoc({ document_id: 'alpha', parent_id: 'p', sort_key: 'a1' }),
    makeDoc({ document_id: 'other', parent_id: 'q', sort_key: 'a0' }),
  ]);
});

describe('treeDragAdapter', () => {
  it('yields the same after_id as before for a fixed sibling fixture', () => {
    const siblings = treeDragAdapter.orderedSiblings('p', 'self');
    // Old contract: filter !is_index + same parent + not self; (sort_key ?? '', id) ASC.
    expect(siblings.map(s => s.id)).toEqual(['gamma', 'alpha', 'beta']);
    // before-half drops land after the PREVIOUS sibling; null = top of the group.
    expect(dropAfterId(siblings, 'gamma', true)).toBeNull();
    expect(dropAfterId(siblings, 'alpha', true)).toBe('gamma');
    // after-half drops land after the hovered sibling.
    expect(dropAfterId(siblings, 'beta', false)).toBe('beta');
    // A row outside the sibling list is not a valid target (undefined = no commit).
    expect(dropAfterId(siblings, 'other', false)).toBeUndefined();
  });
});

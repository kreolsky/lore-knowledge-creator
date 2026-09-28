/**
 * `setDocuments` identity contract.
 *
 * The sidebar flash on document create/delete is a whole-app re-render cascade:
 * every `setDocuments` used to install a new `documents` array identity, and ~10
 * components (plus every tree row) subscribe to that array by reference. A
 * poll that changes nothing must therefore change nothing observable.
 *
 * The obligation these tests pin: bailing out is only safe if the equality check
 * covers the WHOLE document. `patchTree` reads title/content/headings, but the
 * flat `documents` list is what feeds the save-failure marker
 * (`last_save_failed_at`) and the doc-tree key indicator (`key_capabilities`) —
 * comparing patchTree's subset would freeze those silently.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach } from 'vitest';
import { useAppStore } from '../app-store';
import type { Document } from '../../types';

function makeDoc(overrides: Partial<Document> = {}): Document {
  return {
    document_id: 'doc-1',
    project_id: 'proj-1',
    parent_id: null,
    title: 'Doc',
    content: '',
    path: '/doc',
    is_index: false,
    sort_key: 'a0',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

const parent = makeDoc({ document_id: 'p', title: 'Parent', sort_key: 'a0' });
const child = makeDoc({ document_id: 'c', parent_id: 'p', title: 'Child', sort_key: 'a1' });
const sibling = makeDoc({ document_id: 's', title: 'Sibling', sort_key: 'a2' });

/** Fresh objects every time — a caller never hands the store its own array back. */
function freshCopy(docs: Document[]): Document[] {
  return docs.map((d) => ({ ...d }));
}

describe('setDocuments identity', () => {
  beforeEach(() => {
    useAppStore.setState({ documents: [], documentTree: [] });
    useAppStore.getState().setDocuments([parent, child, sibling]);
  });

  it('keeps both identities when a new array carries identical content', () => {
    const prevDocs = useAppStore.getState().documents;
    const prevTree = useAppStore.getState().documentTree;

    useAppStore.getState().setDocuments(freshCopy([parent, child, sibling]));

    expect(useAppStore.getState().documents).toBe(prevDocs);
    expect(useAppStore.getState().documentTree).toBe(prevTree);
  });

  it('installs a new list when only last_save_failed_at differs', () => {
    const prevDocs = useAppStore.getState().documents;

    useAppStore
      .getState()
      .setDocuments(
        freshCopy([parent, child, sibling]).map((d) =>
          d.document_id === 'c' ? { ...d, last_save_failed_at: '2026-08-17T10:00:00Z' } : d,
        ),
      );

    expect(useAppStore.getState().documents).not.toBe(prevDocs);
    expect(
      useAppStore.getState().documents.find((d) => d.document_id === 'c')!.last_save_failed_at,
    ).toBe('2026-08-17T10:00:00Z');
  });

  it('installs a new list when only key_capabilities differs', () => {
    const prevDocs = useAppStore.getState().documents;

    useAppStore
      .getState()
      .setDocuments(
        freshCopy([parent, child, sibling]).map((d) =>
          d.document_id === 's' ? { ...d, key_capabilities: ['read'] } : d,
        ),
      );

    expect(useAppStore.getState().documents).not.toBe(prevDocs);
    expect(
      useAppStore.getState().documents.find((d) => d.document_id === 's')!.key_capabilities,
    ).toEqual(['read']);
  });

  it('patches a title in place and leaves untouched node identities alone', () => {
    const prevTree = useAppStore.getState().documentTree;
    const prevSiblingNode = prevTree.find((n) => n.document_id === 's')!;

    useAppStore
      .getState()
      .setDocuments(
        freshCopy([parent, child, sibling]).map((d) =>
          d.document_id === 'p' ? { ...d, title: 'Renamed' } : d,
        ),
      );

    const tree = useAppStore.getState().documentTree;
    expect(tree).not.toBe(prevTree);
    expect(tree.find((n) => n.document_id === 'p')!.title).toBe('Renamed');
    expect(tree.find((n) => n.document_id === 's')).toBe(prevSiblingNode);
  });

  it('rebuilds the tree on a structural change', () => {
    const prevTree = useAppStore.getState().documentTree;
    const added = makeDoc({ document_id: 'n', parent_id: 'p', title: 'New', sort_key: 'a3' });

    useAppStore.getState().setDocuments([...freshCopy([parent, child, sibling]), added]);

    const tree = useAppStore.getState().documentTree;
    expect(tree).not.toBe(prevTree);
    expect(tree.find((n) => n.document_id === 'p')!.children.map((c) => c.document_id)).toEqual([
      'c',
      'n',
    ]);
  });

  it('makes a delete WS echo over an already-filtered list a no-op', () => {
    // Sidebar removes the doc optimistically, then the WS broadcast filters the
    // same id out of the already-filtered list. The second call must not fire.
    useAppStore.getState().setDocuments(freshCopy([parent, sibling]));
    const afterOptimistic = useAppStore.getState().documents;
    const treeAfterOptimistic = useAppStore.getState().documentTree;

    useAppStore
      .getState()
      .setDocuments(useAppStore.getState().documents.filter((d) => d.document_id !== 'c'));

    expect(useAppStore.getState().documents).toBe(afterOptimistic);
    expect(useAppStore.getState().documentTree).toBe(treeAfterOptimistic);
  });
});

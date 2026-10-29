/** useSiblingDragReorder — tree adapter + drop-indicator pin.
 *
 * The tree adapter resolves drops against the VISIBLE rows read off the DOM:
 * any gap, any depth, the pointer's X picking the depth; the commit sends
 * {after_id} alone while the parent is unchanged and {after_id, parent_id}
 * ('' = root) when it is not. A row's middle half drops INTO it (first child)
 * and the indicator becomes a vertical bar at the row's indent. The indicator is
 * one shared element for both panels: a line with a chevron at EACH end,
 * pointing inward.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { apiClient } from '../api/client';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import {
  treeDragAdapter,
  type DragDropTarget,
} from './useSiblingDragReorder';
import { renderHook } from '@testing-library/react';
import { useSiblingDragReorder, type SiblingDragAdapter } from './useSiblingDragReorder';
import { TREE_INDENT } from './treeDropTarget';
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

// Visible tree fixture (rows in DOM order): R1, A ▸ A2 ▸ A2a,A2b, B.
const LAYOUT: Array<{ id: string; parent: string; level: number }> = [
  { id: 'R1', parent: '', level: 0 },
  { id: 'A', parent: '', level: 0 },
  { id: 'A2', parent: 'A', level: 1 },
  { id: 'A2a', parent: 'A2', level: 2 },
  { id: 'A2b', parent: 'A2', level: 2 },
  { id: 'B', parent: '', level: 0 },
];

const ROW_LEFT = 20;
const ROW_WIDTH = 200;
const ROW_H = 24;
const ROW_TOP = 100;

let tree: HTMLDivElement | null = null;

/** Pointer X that maps to `depth` for a row at ROW_LEFT (round((x-left-4)/12)). */
const xAtDepth = (depth: number) => ROW_LEFT + 4 + depth * TREE_INDENT;

function buildTree(): HTMLDivElement {
  const el = document.createElement('div');
  el.setAttribute('role', 'tree');
  LAYOUT.forEach((r, i) => {
    const row = document.createElement('div');
    row.setAttribute('data-doc-id', r.id);
    row.setAttribute('data-parent-id', r.parent);
    row.setAttribute('data-level', String(r.level));
    const top = ROW_TOP + i * ROW_H;
    vi.spyOn(row, 'getBoundingClientRect').mockReturnValue({
      top, bottom: top + ROW_H, left: ROW_LEFT, right: ROW_LEFT + ROW_WIDTH,
      width: ROW_WIDTH, height: ROW_H, x: ROW_LEFT, y: top, toJSON: () => ({}),
    } as DOMRect);
    el.appendChild(row);
  });
  document.body.appendChild(el);
  return el;
}

const rowEl = (id: string) => tree!.querySelector<HTMLElement>(`[data-doc-id="${id}"]`)!;

beforeEach(() => {
  useAppStore.getState().setDocuments([
    makeDoc({ document_id: 'R1', parent_id: null, sort_key: 'a0' }),
    makeDoc({ document_id: 'A', parent_id: null, sort_key: 'a1' }),
    makeDoc({ document_id: 'A2', parent_id: 'A', sort_key: 'a1a0' }),
    makeDoc({ document_id: 'A2a', parent_id: 'A2', sort_key: 'a1a1' }),
    makeDoc({ document_id: 'A2b', parent_id: 'A2', sort_key: 'a1a2' }),
    makeDoc({ document_id: 'B', parent_id: null, sort_key: 'a2' }),
  ]);
  useUIStore.setState({ collapsedDocIds: [] });
  tree = buildTree();
});

afterEach(() => {
  tree?.remove();
  tree = null;
  vi.restoreAllMocks();
});

describe('treeDragAdapter.dropTarget', () => {
  const drag = { id: 'R1', groupId: '' };

  const drop = (rowId: string, clientX: number, clientY: number): DragDropTarget | undefined =>
    treeDragAdapter.dropTarget(rowEl(rowId), { clientX, clientY }, drag);

  it('the gap above an expanded parent\'s first child nests under it (after=null)', () => {
    // Hover A2's upper half — the gap between A and A2.
    expect(drop('A2', xAtDepth(1), ROW_TOP + 2 * ROW_H + 4)).toMatchObject({ parentId: 'A', afterId: null });
  });
});

// ── Drag sources without a group (gallery tiles, unhosted cards) ─────────────

describe('useSiblingDragReorder — group-less source', () => {
  function setup(withOutside: boolean) {
    const container = document.createElement('div');
    const tile = document.createElement('div');
    tile.setAttribute('data-x-id', 'img-1'); // id, NO group attribute
    container.appendChild(tile);
    document.body.appendChild(container);
    const outsideCtl = { move: vi.fn(), hide: vi.fn(), commit: vi.fn() };
    const adapter: SiblingDragAdapter = {
      rowIdAttr: 'data-x-id',
      groupAttr: 'data-x-group',
      ignoreTarget: () => false,
      dropTarget: vi.fn(() => undefined),
      commit: vi.fn(),
      ...(withOutside ? { outsideTarget: vi.fn(() => outsideCtl) } : {}),
    };
    const ref = { current: container };
    const hook = renderHook(() => useSiblingDragReorder(ref, true, adapter));
    const drag = () => {
      document.elementFromPoint = () => document.body;
      tile.dispatchEvent(new PointerEvent('pointerdown', { button: 0, clientX: 0, clientY: 0, bubbles: true }));
      document.dispatchEvent(new PointerEvent('pointermove', { clientX: 50, clientY: 50 }));
      document.dispatchEvent(new PointerEvent('pointerup', { clientX: 50, clientY: 50 }));
    };
    return { container, adapter, outsideCtl, hook, drag };
  }

  it('drags to the outside surface when the adapter owns one', () => {
    const { container, adapter, outsideCtl, hook, drag } = setup(true);
    drag();
    expect(adapter.outsideTarget).toHaveBeenCalledWith(expect.objectContaining({ id: 'img-1', groupId: null }));
    expect(outsideCtl.move).toHaveBeenCalled();
    expect(outsideCtl.commit).toHaveBeenCalledWith('img-1');
    expect(adapter.commit).not.toHaveBeenCalled();
    hook.unmount(); container.remove();
  });

  it('is not a drag source for a reorder-only adapter', () => {
    const { container, adapter, hook, drag } = setup(false);
    drag();
    expect(adapter.dropTarget).not.toHaveBeenCalled();
    expect(adapter.commit).not.toHaveBeenCalled();
    hook.unmount(); container.remove();
  });
});

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
import { t } from '../i18n';
import {
  treeDragAdapter,
  createDropIndicator,
  placeDropIndicator,
  type DragDropTarget,
} from './useSiblingDragReorder';
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
const doc = (id: string) => useAppStore.getState().documents.find(d => d.document_id === id);

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

  it('a gap between siblings at depth 2 keeps the parent and lands after the hovered row', () => {
    // Hover A2b's upper half at depth-2 X → after A2a, still under A2.
    const target = drop('A2b', xAtDepth(2), ROW_TOP + 4 * ROW_H + 4);
    expect(target).toBeDefined();
    expect(target!.parentId).toBe('A2');
    expect(target!.afterId).toBe('A2a');
    expect(target!.lineLeft).toBe(ROW_LEFT + 4 + 2 * TREE_INDENT);
    expect(target!.lineRight).toBe(ROW_LEFT + ROW_WIDTH - 5);
    expect(target!.lineY).toBe(ROW_TOP + 4 * ROW_H);
  });

  it('the end-of-subtree gap picks the ancestor/after from the pointer X depth', () => {
    // Hover B's upper half — the gap after A2's subtree. B is row 5.
    const y = ROW_TOP + 5 * ROW_H + 4;
    expect(drop('B', xAtDepth(0), y)).toMatchObject({ parentId: null, afterId: 'A' });
    expect(drop('B', xAtDepth(1), y)).toMatchObject({ parentId: 'A', afterId: 'A2' });
    expect(drop('B', xAtDepth(2), y)).toMatchObject({ parentId: 'A2', afterId: 'A2b' });
  });

  it('the gap above an expanded parent\'s first child nests under it (after=null)', () => {
    // Hover A2's upper half — the gap between A and A2.
    expect(drop('A2', xAtDepth(1), ROW_TOP + 2 * ROW_H + 4)).toMatchObject({ parentId: 'A', afterId: null });
  });

  it('the very top gap is root/top; the dragged row itself is never a target', () => {
    // Drag B over R1's upper half — the gap above the first visible row.
    const dragB = { id: 'B', groupId: '' };
    expect(treeDragAdapter.dropTarget(rowEl('R1'), { clientX: xAtDepth(0), clientY: ROW_TOP }, dragB))
      .toMatchObject({ parentId: null, afterId: null });
    // Hovering the dragged row itself (either half) never offers a target.
    expect(drop('R1', xAtDepth(1), ROW_TOP + 4)).toBeUndefined();
  });

  it('gaps inside the dragged subtree resolve to undefined (no line)', () => {
    const dragA = { id: 'A', groupId: '' };
    // Gap between A2a and A2b while dragging A.
    const y = ROW_TOP + 4 * ROW_H + 4;
    expect(treeDragAdapter.dropTarget(rowEl('A2b'), { clientX: xAtDepth(2), clientY: y }, dragA)).toBeUndefined();
    // Gap right under A (above A2) would nest under A itself.
    expect(treeDragAdapter.dropTarget(rowEl('A2'), { clientX: xAtDepth(1), clientY: ROW_TOP + 2 * ROW_H + 4 }, dragA)).toBeUndefined();
  });
});

describe('treeDragAdapter.dropTarget — drop INTO a row', () => {
  const drag = { id: 'R1', groupId: '' };
  const mid = (i: number) => ROW_TOP + i * ROW_H + ROW_H / 2;

  it('the middle half of a row nests into it as first child, with a bar at its indent', () => {
    // A2b (row 4, level 2) is a leaf — still a nest target.
    const target = treeDragAdapter.dropTarget(rowEl('A2b'), { clientX: xAtDepth(0), clientY: mid(4) }, drag);
    expect(target).toEqual({
      parentId: 'A2b', afterId: null,
      lineLeft: ROW_LEFT + 4 + 2 * TREE_INDENT,
      lineRight: ROW_LEFT + 4 + 2 * TREE_INDENT + 4,
      lineY: ROW_TOP + 4 * ROW_H,
      // Inset by half a chevron: each tip lands on the row's top/bottom edge.
      nest: { top: ROW_TOP + 4 * ROW_H + 5, bottom: ROW_TOP + 5 * ROW_H - 5 },
    });
  });

  it('the zone edges: the outer quarters stay "between", the middle half is "inside"', () => {
    const top = ROW_TOP + 5 * ROW_H; // B, row 5
    const at = (y: number) => treeDragAdapter.dropTarget(rowEl('B'), { clientX: xAtDepth(0), clientY: y }, drag);
    expect(at(top + ROW_H * 0.25 - 1)?.nest).toBeUndefined();
    expect(at(top + ROW_H * 0.25)?.nest).toBeDefined();
    expect(at(top + ROW_H * 0.75 - 1)?.nest).toBeDefined();
    expect(at(top + ROW_H * 0.75)?.nest).toBeUndefined();
  });

  it('the dragged row and its descendants are not nest targets', () => {
    const dragA = { id: 'A', groupId: '' };
    expect(treeDragAdapter.dropTarget(rowEl('A'), { clientX: xAtDepth(0), clientY: mid(1) }, dragA)).toBeUndefined();
    expect(treeDragAdapter.dropTarget(rowEl('A2a'), { clientX: xAtDepth(0), clientY: mid(3) }, dragA)).toBeUndefined();
  });
});

describe('treeDragAdapter.commit', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('parent unchanged: PATCHes {after_id} only and reconciles the key', async () => {
    const patch = vi.spyOn(apiClient, 'patch').mockResolvedValue({ document_id: 'R1', sort_key: 'a1V' });
    treeDragAdapter.commit('R1', { parentId: null, afterId: 'A', lineLeft: 0, lineRight: 0, lineY: 0 });
    // Provisional key lands synchronously — optimistic same-group reorder.
    expect(doc('R1')?.sort_key).toBe('a1V');
    expect(doc('R1')?.parent_id).toBeNull();
    await vi.waitFor(() => expect(patch).toHaveBeenCalledWith('/documents/R1/reorder', { after_id: 'A' }));
  });

  it('parent changed: PATCHes {after_id, parent_id} and re-buckets optimistically', async () => {
    const patch = vi.spyOn(apiClient, 'patch').mockResolvedValue({ document_id: 'R1', parent_id: 'A', sort_key: 'a1aV' });
    treeDragAdapter.commit('R1', { parentId: 'A', afterId: null, lineLeft: 0, lineRight: 0, lineY: 0 });
    // Optimistic: parent + provisional key ('' = top) in one write.
    expect(doc('R1')?.parent_id).toBe('A');
    expect(doc('R1')?.sort_key).toBe('');
    await vi.waitFor(() => {
      expect(doc('R1')?.sort_key).toBe('a1aV');
      expect(doc('R1')?.parent_id).toBe('A');
    });
    expect(patch).toHaveBeenCalledWith('/documents/R1/reorder', { after_id: null, parent_id: 'A' });
  });

  it('parent changed to root: parent_id is sent as \'\'', async () => {
    const patch = vi.spyOn(apiClient, 'patch').mockResolvedValue({ document_id: 'A2', parent_id: null, sort_key: 'a2V' });
    treeDragAdapter.commit('A2', { parentId: null, afterId: 'B', lineLeft: 0, lineRight: 0, lineY: 0 });
    await vi.waitFor(() => expect(patch).toHaveBeenCalledWith('/documents/A2/reorder', { after_id: 'B', parent_id: '' }));
  });

  it('a move under a new parent expands it, so the moved doc stays visible', async () => {
    useUIStore.setState({ collapsedDocIds: ['A2b', 'B'] });
    vi.spyOn(apiClient, 'patch').mockResolvedValue({ document_id: 'R1', parent_id: 'A2b', sort_key: 'x' });
    treeDragAdapter.commit('R1', {
      parentId: 'A2b', afterId: null, lineLeft: 0, lineRight: 0, lineY: 0, nest: { top: 0, bottom: 0 },
    });
    expect(useUIStore.getState().collapsedDocIds).toEqual(['B']);
  });

  it('a failed move rolls back parent AND key, and toasts', async () => {
    vi.spyOn(apiClient, 'patch').mockRejectedValue(new Error('boom'));
    treeDragAdapter.commit('R1', { parentId: 'A2', afterId: 'A2a', lineLeft: 0, lineRight: 0, lineY: 0 });
    await vi.waitFor(() => {
      expect(doc('R1')?.parent_id).toBeNull();
      expect(doc('R1')?.sort_key).toBe('a0');
    });
    expect(useAppStore.getState().toast?.message).toBe(t('failedToReorderDocument'));
  });
});

describe('drop indicator', () => {
  it('carries TWO mirrored chevrons pointing inward — one shared element for both panels', () => {
    const el = createDropIndicator();
    expect(el.getAttribute('data-drop-indicator')).toBe('');
    const left = el.querySelector<SVGElement>('svg[data-chevron="left"]')!;
    const right = el.querySelector<SVGElement>('svg[data-chevron="right"]')!;
    expect(left.style.left).toBe('-5px');
    expect(left.style.right).toBe('');
    expect(left.querySelector('polyline')!.getAttribute('points')).toBe('1,1 5,5 1,9');
    expect(right.style.right).toBe('-5px');
    expect(right.style.left).toBe('');
    expect(right.querySelector('polyline')!.getAttribute('points')).toBe('9,1 5,5 9,9');
  });

  it('straddles the gap between the given left/right ends at y', () => {
    const el = createDropIndicator();
    expect(el.style.display).toBe('none');
    placeDropIndicator(el, { lineLeft: 48, lineRight: 215, lineY: 196 });
    expect(el.style.display).toBe('block');
    expect(el.style.top).toBe('194px');
    expect(el.style.left).toBe('48px');
    expect(el.style.width).toBe('167px');
  });

  it('a nest target turns it into a vertical 4px stroke with top/bottom chevrons; a gap restores the line', () => {
    const el = createDropIndicator();
    const shown = () => Array.from(el.querySelectorAll<SVGElement>('svg[data-chevron]'))
      .filter(svg => svg.style.display !== 'none').map(svg => svg.dataset.chevron).sort();
    const top = el.querySelector<SVGElement>('svg[data-chevron="top"]')!;
    const bottom = el.querySelector<SVGElement>('svg[data-chevron="bottom"]')!;
    // Mirrored pair, tips pointing INTO the stroke: 'v' at the top, '^' at the bottom.
    expect(top.querySelector('polyline')!.getAttribute('points')).toBe('1,1 5,5 9,1');
    expect(bottom.querySelector('polyline')!.getAttribute('points')).toBe('1,9 5,5 9,9');
    expect(top.style.top).toBe('-5px');
    expect(bottom.style.bottom).toBe('-5px');
    expect(top.style.left).toBe('-3px');

    placeDropIndicator(el, { lineLeft: 48, lineRight: 52, lineY: 100, nest: { top: 105, bottom: 119 } });
    expect(el.style.top).toBe('105px');
    expect(el.style.left).toBe('48px');
    expect(el.style.width).toBe('4px');
    expect(el.style.height).toBe('14px');
    expect(shown()).toEqual(['bottom', 'top']);

    placeDropIndicator(el, { lineLeft: 48, lineRight: 215, lineY: 196 });
    expect(el.style.height).toBe('4px');
    expect(el.style.width).toBe('167px');
    expect(shown()).toEqual(['left', 'right']);
  });
});

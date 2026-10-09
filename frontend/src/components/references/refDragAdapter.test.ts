/** refDragAdapter — reference-card half of the sibling-drag seam.
 *
 * Pins: the drop stays INSIDE the host group (same group, live-only, ordered
 * by (sort_key, id)); a pointerdown on a RefCard action button or the
 * inline-rename input never starts a drag; commit is
 * optimistic-provisional → PATCH reconcile → rollback.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { apiClient } from '../../api/client';
import { useAppStore } from '../../store/app-store';
import { t } from '../../i18n';
import { refDragAdapter } from './refDragAdapter';
import { createDropIndicator, placeDropIndicator, rowDropLine } from '../../hooks/useSiblingDragReorder';
import type { Reference } from '../../types';

function makeRef(overrides: Partial<Reference> = {}): Reference {
  return {
    reference_id: 'ref-1',
    project_id: 'proj-1',
    document_id: 'child',
    title: 'Ref',
    media_type: 'markdown',
    source_url: null,
    processing_status: null,
    file_path: null,
    file_meta: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  };
}

let patchSpy: ReturnType<typeof vi.spyOn> | null = null;
let host: HTMLDivElement | null = null;

/** A RefCard row with a mocked rect (top advances per call order). */
function cardRow(id: string, group: string | undefined, top: number): HTMLElement {
  const row = document.createElement('div');
  row.setAttribute('data-ref-id', id);
  if (group !== undefined) row.setAttribute('data-ref-group', group);
  vi.spyOn(row, 'getBoundingClientRect').mockReturnValue({
    top, bottom: top + 20, left: 40, right: 340, width: 300, height: 20,
    x: 40, y: top, toJSON: () => ({}),
  } as DOMRect);
  return row;
}

beforeEach(() => {
  useAppStore.getState().setReferences([
    makeRef({ reference_id: 'own-a', document_id: 'child', sort_key: 'a0' }),
    makeRef({ reference_id: 'own-b', document_id: 'child', sort_key: 'a2' }),
    makeRef({ reference_id: 'par-a', document_id: 'parent', sort_key: 'b2' }),
    makeRef({ reference_id: 'arch', document_id: 'child', sort_key: 'a9', archived: true }),
  ]);
  host = document.createElement('div');
  host.append(
    cardRow('own-a', 'child', 100),
    cardRow('own-b', 'child', 120),
    cardRow('par-a', 'parent', 140),
    cardRow('arch', 'child', 160),
  );
  document.body.appendChild(host);
});

afterEach(() => {
  patchSpy?.mockRestore();
  patchSpy = null;
  host?.remove();
  host = null;
  vi.restoreAllMocks();
});

describe('refDragAdapter.dropTarget', () => {
  const drag = { id: 'own-b', groupId: 'child' };

  it('resolves inside the live host group only, ordered by (sort_key, id)', () => {
    const row = (id: string) => host!.querySelector<HTMLElement>(`[data-ref-id="${id}"]`)!;
    // own-b's upper half over own-a → before it → top of the group (after=null).
    expect(refDragAdapter.dropTarget(row('own-a'), { clientX: 100, clientY: 105 }, drag))
      .toMatchObject({ parentId: null, afterId: null });
    // Lower half of own-a → after own-a.
    expect(refDragAdapter.dropTarget(row('own-a'), { clientX: 100, clientY: 115 }, drag))
      .toMatchObject({ parentId: null, afterId: 'own-a' });
    // A foreign-group card, an archived card and the dragged card are not targets.
    expect(refDragAdapter.dropTarget(row('par-a'), { clientX: 100, clientY: 145 }, drag)).toBeUndefined();
    expect(refDragAdapter.dropTarget(row('arch'), { clientX: 100, clientY: 165 }, drag)).toBeUndefined();
    expect(refDragAdapter.dropTarget(row('own-b'), { clientX: 100, clientY: 125 }, drag)).toBeUndefined();
  });

  it('spans the row with the line geometry shared with the tree (both chevrons)', () => {
    const row = host!.querySelector<HTMLElement>('[data-ref-id="own-a"]')!;
    const target = refDragAdapter.dropTarget(row, { clientX: 100, clientY: 115 }, drag)!;
    const line = rowDropLine({ top: 100, bottom: 120, left: 40, right: 340 } as DOMRect, false);
    expect({ lineLeft: target.lineLeft, lineRight: target.lineRight, lineY: target.lineY }).toEqual(line);
    // …and that geometry drives the ONE shared two-chevron indicator.
    const el = createDropIndicator();
    placeDropIndicator(el, target);
    const shown = Array.from(el.querySelectorAll<SVGElement>('svg[data-chevron]'))
      .filter(svg => svg.style.display !== 'none').map(svg => svg.dataset.chevron).sort();
    expect(shown).toEqual(['left', 'right']);
  });
});

describe('refDragAdapter.ignoreTarget', () => {
  it('a pointerdown on a RefCard action button or the rename input starts no drag', () => {
    const row = document.createElement('div');
    document.body.appendChild(row);
    const btn = document.createElement('button');
    const input = document.createElement('input');
    const body = document.createElement('span');
    row.append(btn, input, body);
    expect(refDragAdapter.ignoreTarget(btn)).toBe(true);
    expect(refDragAdapter.ignoreTarget(input)).toBe(true);
    expect(refDragAdapter.ignoreTarget(body)).toBe(false);
    row.remove();
  });
});

describe('refDragAdapter.commit', () => {
  it('applies a provisional key, PATCHes the reorder route, reconciles with the server key', async () => {
    patchSpy = vi.spyOn(apiClient, 'patch').mockResolvedValue({ document_id: 'own-b', sort_key: 'a1V' });
    refDragAdapter.commit('own-b', { parentId: null, afterId: 'own-a', lineLeft: 0, lineRight: 0, lineY: 0 });
    // Provisional key lands synchronously (optimistic) — a0 < a0V < a2 keeps the slot.
    expect(useAppStore.getState().references.find(r => r.reference_id === 'own-b')?.sort_key).toBe('a0V');
    await vi.waitFor(() => {
      expect(useAppStore.getState().references.find(r => r.reference_id === 'own-b')?.sort_key).toBe('a1V');
    });
    expect(patchSpy).toHaveBeenCalledWith('/documents/own-b/reorder', { after_id: 'own-a' });
  });

  it('rolls back to the old key and toasts on failure', async () => {
    patchSpy = vi.spyOn(apiClient, 'patch').mockRejectedValue(new Error('boom'));
    refDragAdapter.commit('own-b', { parentId: null, afterId: null, lineLeft: 0, lineRight: 0, lineY: 0 });
    await vi.waitFor(() => {
      expect(useAppStore.getState().references.find(r => r.reference_id === 'own-b')?.sort_key).toBe('a2');
    });
    expect(useAppStore.getState().toast?.message).toBe(t('failedToReorderReference'));
  });
});

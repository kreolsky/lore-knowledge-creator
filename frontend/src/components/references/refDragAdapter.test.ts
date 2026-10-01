/** refDragAdapter — reference-card half of the sibling-drag seam.
 *
 * Pins: the group is (document_id, live-only) and orders by (sort_key, id); a
 * pointerdown on a RefCard action button or the inline-rename input never
 * starts a drag; commit is optimistic-provisional → PATCH reconcile → rollback.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { apiClient } from '../../api/client';
import { useAppStore } from '../../store/app-store';
import { t } from '../../i18n';
import { refDragAdapter } from './refDragAdapter';
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

beforeEach(() => {
  useAppStore.getState().setReferences([
    makeRef({ reference_id: 'own-a', document_id: 'child', sort_key: 'a0' }),
    makeRef({ reference_id: 'own-b', document_id: 'child', sort_key: 'a2' }),
    makeRef({ reference_id: 'par-a', document_id: 'parent', sort_key: 'b2' }),
    makeRef({ reference_id: 'arch', document_id: 'child', sort_key: 'a9', archived: true }),
  ]);
});

afterEach(() => {
  patchSpy?.mockRestore();
  patchSpy = null;
});

describe('refDragAdapter.orderedSiblings', () => {
  it('is the live refs of the group in (sort_key, id) order — archived, foreign and self excluded', () => {
    const siblings = refDragAdapter.orderedSiblings('child', 'own-b');
    expect(siblings.map(s => s.id)).toEqual(['own-a']);
    const siblingsAll = refDragAdapter.orderedSiblings('child', 'missing');
    expect(siblingsAll.map(s => s.id)).toEqual(['own-a', 'own-b']);
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
    refDragAdapter.commit('own-b', 'own-a');
    // Provisional key lands synchronously (optimistic) — a0 < a0V < a2 keeps the slot.
    expect(useAppStore.getState().references.find(r => r.reference_id === 'own-b')?.sort_key).toBe('a0V');
    await vi.waitFor(() => {
      expect(useAppStore.getState().references.find(r => r.reference_id === 'own-b')?.sort_key).toBe('a1V');
    });
    expect(patchSpy).toHaveBeenCalledWith('/documents/own-b/reorder', { after_id: 'own-a' });
  });

  it('rolls back to the old key and toasts on failure', async () => {
    patchSpy = vi.spyOn(apiClient, 'patch').mockRejectedValue(new Error('boom'));
    refDragAdapter.commit('own-b', null);
    await vi.waitFor(() => {
      expect(useAppStore.getState().references.find(r => r.reference_id === 'own-b')?.sort_key).toBe('a2');
    });
    expect(useAppStore.getState().toast?.message).toBe(t('failedToReorderReference'));
  });
});

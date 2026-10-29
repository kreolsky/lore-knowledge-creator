/** refDragAdapter — reference-card half of the sibling-drag seam.
 *
 * Pins: the drop stays INSIDE the host group (same group, live-only, ordered
 * by (sort_key, id)); a pointerdown on a RefCard action button or the
 * inline-rename input never starts a drag; commit is
 * optimistic-provisional → PATCH reconcile → rollback.
 *
 * Outside surface (plan image-block-in-document step 4): a card dragged over
 * the document editor resolves the drop view from pointer coordinates
 * (editable gate — readonly/commentator/public views are no drop surfaces),
 * shows the drop caret there, and commits the `![title](ref:id)` embed INLINE
 * at the drop position in THAT view (the drop view, never the focused slot).
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { EditorView } from '@codemirror/view';
import { EditorState } from '@codemirror/state';
import { useAppStore } from '../../store/app-store';
import { refDragAdapter } from './refDragAdapter';
import { dropCaret } from '../editor/drop-caret';
import type { Reference } from '../../types';

vi.mock('mermaid', () => ({
  default: { initialize: vi.fn(), render: vi.fn(async () => ({ svg: '<svg></svg>' })) },
}));

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

// ── Harness for the editor-drop half ────────────────────────────────────────

const views: EditorView[] = [];

function makeView(doc: string, editable = true): EditorView {
  const view = new EditorView({
    state: EditorState.create({
      doc,
      extensions: [EditorView.editable.of(editable), dropCaret()],
    }),
    parent: document.body,
  });
  views.push(view);
  return view;
}

/** Point document.elementFromPoint at `el` and view.posAtCoords at `pos`.
 * (jsdom implements neither — both are stubbed directly.) */
function aim(el: Element, pos = 0, view?: EditorView) {
  document.elementFromPoint = () => el;
  if (view) vi.spyOn(view, 'posAtCoords').mockReturnValue(pos);
}

function caret(view: EditorView): Element | null {
  return view.dom.querySelector('[data-drop-caret]');
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
  // WHY: jsdom has no elementFromPoint; nothing under the pointer until a test aims.
  document.elementFromPoint = () => null;
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
  while (views.length) views.pop()!.destroy();
});

describe('refDragAdapter.outsideTarget — editor drop controller', () => {
  const drag = { id: 'own-a', groupId: 'child' };

  it('a pointer-following ghost + copy/no-drop cursor; hide and commit remove both', () => {
    const view = makeView('hello world');
    const ghost = () => document.querySelector('[data-ref-drag-ghost]') as HTMLElement | null;
    const ctl = refDragAdapter.outsideTarget!(drag);
    expect(ghost()).not.toBeNull();
    ctl.move({ clientX: 30, clientY: 40 });
    expect(ghost()!.style.transform).toBe('translate(42px, 52px)');
    expect(document.body.classList.contains('ref-drag-none')).toBe(true);
    aim(view.dom, 5, view);
    ctl.move({ clientX: 10, clientY: 10 });
    expect(document.body.classList.contains('ref-drag-copy')).toBe(true);
    ctl.hide();
    expect(ghost()).toBeNull();
    expect(document.body.className).not.toMatch(/ref-drag-/);
    const ctl2 = refDragAdapter.outsideTarget!(drag);
    ctl2.commit(drag.id);
    expect(ghost()).toBeNull();
  });

  it('switching editors (split view) clears the OTHER view\'s caret', () => {
    const left = makeView('left');
    const right = makeView('right');
    const ctl = refDragAdapter.outsideTarget!(drag);
    aim(left.dom, 1, left);
    ctl.move({ clientX: 10, clientY: 10 });
    expect(caret(left)).not.toBeNull();
    aim(right.dom, 2, right);
    ctl.move({ clientX: 500, clientY: 10 });
    expect(caret(left)).toBeNull();
    expect(caret(right)).not.toBeNull();
    ctl.hide();
    expect(caret(right)).toBeNull();
  });
});

/**
 * RefPanelPlaque — the parent slot (goto / change / empty), the always-present
 * download menu, and the stateful trash (live → archive on one click; archived →
 * armed two-click delete) with the restore icon beside it.
 */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import type { Reference } from '../../types';
import { RefPanelPlaque } from './RefPanelPlaque';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock('../../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
vi.mock('../../store/app-store', () => ({
  useAppStore: (selector: (s: { documents: Array<{ document_id: string; title: string }> }) => unknown) =>
    selector({ documents: [{ document_id: 'doc-1', title: 'Doc One' }, { document_id: 'doc-2', title: 'Doc Two' }] }),
}));
vi.mock('../ui', () => ({
  Button: (props: { onClick?: () => void; children?: React.ReactNode }) => <button data-back onClick={props.onClick}>{props.children}</button>,
  IconButton: (props: { title?: string; onClick?: (e: React.MouseEvent<HTMLButtonElement>) => void; onMouseLeave?: () => void; children?: React.ReactNode }) => (
    <button title={props.title} onClick={props.onClick} onMouseLeave={props.onMouseLeave}>{props.children}</button>
  ),
  DownloadMenu: (props: { documentId: string; hasText: boolean; original?: { name: string } }) => (
    <div data-testid="download" data-has-text={String(props.hasText)} data-original={props.original?.name ?? ''} />
  ),
  FieldInput: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
}));

const NOW = '2026-01-01T00:00:00Z';
const REF: Reference = {
  reference_id: 'ref-1', project_id: 'p1', document_id: 'doc-1', title: 'Ref One', media_type: 'markdown',
  source_url: null, content: 'body', processing_status: 'ready', file_path: null, file_meta: null,
  created_at: NOW, updated_at: NOW,
};

let container: HTMLDivElement;
let root: Root;
const handlers = {
  onBack: vi.fn(), onExitPanel: vi.fn(), onGotoParent: vi.fn(), onChangeParent: vi.fn(), onArchive: vi.fn(), onRestore: vi.fn(), onDelete: vi.fn(),
  onStartRename: vi.fn(), onRenameChange: vi.fn(), onRenameCommit: vi.fn(), onRenameCancel: vi.fn(),
};

beforeEach(() => {
  Object.values(handlers).forEach(h => h.mockReset());
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); });

function render(ref: Reference, opts: { canEdit?: boolean; currentDocumentId?: string | null; renamingId?: string | null; renameValue?: string } = {}) {
  act(() => root.render(
    <RefPanelPlaque
      reference={ref}
      canEdit={opts.canEdit ?? true}
      currentDocumentId={opts.currentDocumentId === undefined ? 'doc-1' : opts.currentDocumentId}
      renamingId={opts.renamingId ?? null}
      renameValue={opts.renameValue ?? ''}
      {...handlers}
    />,
  ));
}
const byTitle = (t: string) => container.querySelector(`button[title="${t}"]`) as HTMLButtonElement | null;
const click = (el: Element) => act(() => { el.dispatchEvent(new MouseEvent('click', { bubbles: true })); });

describe('RefPanelPlaque — parent slot', () => {
  it('parent == open document → change-parent icon, click hands the ref + anchor rect up', () => {
    render(REF);
    const slot = container.querySelector('[data-title-slot]')!;
    expect(slot.querySelectorAll('button').length).toBe(1);
    expect(byTitle('gotoParentDoc')).toBeNull();
    click(byTitle('changeParentDocument')!);
    expect(handlers.onChangeParent).toHaveBeenCalledTimes(1);
    expect(handlers.onChangeParent.mock.calls[0][0]).toBe(REF);
    // jsdom returns a plain rect object, not a DOMRect instance — assert the shape.
    expect(handlers.onChangeParent.mock.calls[0][1]).toHaveProperty('width');
  });

  it('parent ≠ open document → goto-parent icon, click navigates to the parent id', () => {
    render(REF, { currentDocumentId: 'doc-2' });
    expect(byTitle('changeParentDocument')).toBeNull();
    click(byTitle('gotoParentDoc')!);
    expect(handlers.onGotoParent).toHaveBeenCalledWith('doc-1');
  });

  it('project-level reference → the slot is rendered and empty', () => {
    render({ ...REF, document_id: null as unknown as string });
    const slot = container.querySelector('[data-title-slot]')!;
    expect(slot).not.toBeNull();
    expect(slot.querySelectorAll('button').length).toBe(0);
  });

  it('read-only → no parent icons, no trash, download still present, back still works', () => {
    render(REF, { canEdit: false });
    expect(container.querySelector('[data-title-slot]')!.querySelectorAll('button').length).toBe(0);
    expect(byTitle('archiveReference')).toBeNull();
    expect(container.querySelector('[data-testid="download"]')).not.toBeNull();
    click(container.querySelector('[data-back]')!);
    expect(handlers.onBack).toHaveBeenCalledTimes(1);
  });

  it('pressed eye left of download → onExitPanel (visible read-only too)', () => {
    render(REF, { canEdit: false });
    const eye = byTitle('toggleRefInPanel')!;
    expect(eye.nextElementSibling).toBe(container.querySelector('[data-testid="download"]'));
    click(eye);
    expect(handlers.onExitPanel).toHaveBeenCalledTimes(1);
  });
});

describe('RefPanelPlaque — trash / restore', () => {
  it('live reference: ONE click archives, nothing is deleted', () => {
    render(REF);
    expect(byTitle('restoreReference')).toBeNull();
    click(byTitle('archiveReference')!);
    expect(handlers.onArchive).toHaveBeenCalledWith(REF);
    expect(handlers.onDelete).not.toHaveBeenCalled();
  });

  it('archived reference: first click arms, second click deletes; restore icon beside it', () => {
    const archived = { ...REF, archived: true };
    render(archived);
    expect(byTitle('archiveReference')).toBeNull();
    click(byTitle('deleteReferencePermanently')!);
    expect(handlers.onDelete).not.toHaveBeenCalled();
    click(byTitle('deleteReferencePermanently')!);
    expect(handlers.onDelete).toHaveBeenCalledWith('ref-1');
    expect(handlers.onArchive).not.toHaveBeenCalled();
    click(byTitle('restoreReference')!);
    expect(handlers.onRestore).toHaveBeenCalledWith(archived);
    // Order: download, then restore DIRECTLY left of the trash.
    const download = container.querySelector('[data-testid="download"]')!;
    const restore = byTitle('restoreReference')!;
    const trash = byTitle('deleteReferencePermanently')!;
    expect(download.compareDocumentPosition(restore) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(restore.nextElementSibling).toBe(trash);
  });
});

describe('RefPanelPlaque — download menu scope', () => {
  const menuOriginal = () => container.querySelector('[data-testid="download"]')!.getAttribute('data-original');

  it.each(['audio', 'image'] as const)('%s ref: no original item in the menu (download lives on the media)', (mediaType) => {
    render({ ...REF, media_type: mediaType, file_path: '/a.bin', file_meta: { original_name: 'a.bin' } as Reference['file_meta'] });
    expect(menuOriginal()).toBe('');
  });

  it('converted PDF: the original item stays — no media surface hosts it', () => {
    render({ ...REF, file_path: '/p.pdf', file_meta: { original_name: 'p.pdf' } as Reference['file_meta'] });
    expect(menuOriginal()).toBe('p.pdf');
  });
});

describe('RefPanelPlaque — title rename', () => {
  const title = () => container.querySelector('[data-ref-title]') as HTMLElement | null;
  const input = () => container.querySelector('[data-rename-input]') as HTMLInputElement | null;
  const dblclick = (el: Element) => act(() => { el.dispatchEvent(new MouseEvent('dblclick', { bubbles: true })); });
  const key = (el: Element, k: string) => act(() => { el.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true })); });

  it('editor: double-click on the title starts the rename with this reference', () => {
    render(REF);
    expect(title()!.title).toContain('chatRenameHint');
    dblclick(title()!);
    expect(handlers.onStartRename).toHaveBeenCalledWith(REF);
  });

  it('read-only: double-click is ignored and the tooltip carries no rename hint', () => {
    render(REF, { canEdit: false });
    expect(title()!.title).toBe('Ref One');
    dblclick(title()!);
    expect(handlers.onStartRename).not.toHaveBeenCalled();
  });

  it('renaming THIS reference → input replaces the title; Enter commits, Escape cancels, blur commits', () => {
    render(REF, { renamingId: 'ref-1', renameValue: 'Draft' });
    expect(title()).toBeNull();
    expect(input()!.value).toBe('Draft');
    key(input()!, 'Enter');
    expect(handlers.onRenameCommit).toHaveBeenCalledWith(REF);
    key(input()!, 'Escape');
    expect(handlers.onRenameCancel).toHaveBeenCalledTimes(1);
    act(() => { input()!.dispatchEvent(new FocusEvent('focusout', { bubbles: true })); });
    expect(handlers.onRenameCommit).toHaveBeenCalledTimes(2);
  });

  it('renaming ANOTHER reference (list card) → the plaque keeps its static title', () => {
    render(REF, { renamingId: 'ref-other', renameValue: 'x' });
    expect(input()).toBeNull();
    expect(title()!.textContent).toBe('Ref One');
  });
});

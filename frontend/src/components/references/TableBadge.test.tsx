/** Tests for TableBadge — inline rename UI (mirrors RefCard rename). Plan: table-rename-badges. */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createRoot } from 'react-dom/client';
import { act } from 'react';
import type { DocumentTableEntry } from '../editor/live-preview/table-block-model';
import { TableBadge } from './TableBadge';

// Stub child components / hooks. FieldInput is rendered as a real <input> so the rename
// keydown/commit/cancel contract is exercisable (the production FieldInput forwards these
// handlers verbatim).
vi.mock('../ui', () => ({
  IconButton: (props: { title?: string; onClick?: (e: any) => void; children?: any }) => (
    <button data-title={props.title} onClick={props.onClick}>{props.children}</button>
  ),
  FieldInput: (props: {
    value?: string;
    onChange?: (e: any) => void;
    onKeyDown?: (e: any) => void;
    onBlur?: () => void;
    onClick?: (e: any) => void;
  }) => (
    <input
      data-testid="table-rename-input"
      value={props.value}
      onChange={props.onChange}
      onKeyDown={props.onKeyDown}
      onBlur={props.onBlur}
      onClick={props.onClick}
    />
  ),
  ListPill: (props: { children?: any }) => <div>{props.children}</div>,
  RowActions: (props: { menu?: any }) => <div>{props.menu}</div>,
}));
vi.mock('../../hooks/useArmedAction', () => ({
  useArmedAction: () => ({ armed: false, arm: () => {}, disarm: () => {}, handleClick: (_cb: () => void) => {} }),
}));
vi.mock('../../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));

function baseEntry(over: Partial<DocumentTableEntry> = {}): DocumentTableEntry {
  return { table_id: 't1', label: 'Old name', rows: 2, cols: 3, unlinked: false, ...over };
}

function renderBadge(props: Partial<Parameters<typeof TableBadge>[0]> & { entry?: DocumentTableEntry }) {
  const entry = props.entry ?? baseEntry();
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => {
    root.render(
      <TableBadge
        entry={entry}
        isActive={false}
        canEdit={true}
        renamingId={null}
        renameValue=""
        onSelect={() => {}}
        onDelete={() => {}}
        onInsert={() => {}}
        onDownload={() => {}}
        onStartRename={() => {}}
        onRenameChange={() => {}}
        onRenameCommit={() => {}}
        onRenameCancel={() => {}}
        {...props}
      />,
    );
  });
  return container;
}

describe('TableBadge rename UI', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
  });

  it('renders the rename (Pencil) action when canEdit', () => {
    const el = renderBadge({ canEdit: true });
    expect(el.querySelector('[data-title="tableBadgeRename"]')).toBeTruthy();
  });

  it('does NOT render the rename action when canEdit is false (Viewer/Commentator)', () => {
    const el = renderBadge({ canEdit: false });
    expect(el.querySelector('[data-title="tableBadgeRename"]')).toBeNull();
  });

  it('shows the rename input when renamingId matches the table id', () => {
    const el = renderBadge({ renamingId: 't1', renameValue: 'typed' });
    const input = el.querySelector('[data-testid="table-rename-input"]') as HTMLInputElement | null;
    expect(input).toBeTruthy();
    expect(input!.value).toBe('typed');
  });

  it('does NOT show the rename input when renamingId is a different table', () => {
    const el = renderBadge({ renamingId: 'other', renameValue: 'typed' });
    expect(el.querySelector('[data-testid="table-rename-input"]')).toBeNull();
  });

  it('Enter commits the rename via onRenameCommit (panel handler normalizes)', () => {
    const commit = vi.fn();
    const entry = baseEntry();
    const el = renderBadge({ renamingId: 't1', renameValue: 'New name', onRenameCommit: commit, entry });
    const input = el.querySelector('[data-testid="table-rename-input"]')!;
    act(() => {
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    });
    expect(commit).toHaveBeenCalledTimes(1);
    expect(commit).toHaveBeenCalledWith(entry);
  });

  it('Escape cancels the rename via onRenameCancel', () => {
    const cancel = vi.fn();
    const el = renderBadge({ renamingId: 't1', renameValue: 'x', onRenameCancel: cancel });
    const input = el.querySelector('[data-testid="table-rename-input"]')!;
    act(() => {
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    });
    expect(cancel).toHaveBeenCalledTimes(1);
  });
});

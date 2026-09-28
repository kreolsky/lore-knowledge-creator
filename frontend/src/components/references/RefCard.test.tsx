/** Tests for RefCard — insert-embed icon visibility (transclusion authoring). */

// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createRoot } from 'react-dom/client';
import { act } from 'react';
import type { Reference } from '../../types';
import { RefCard } from './RefCard';

// Stub child components / hooks that pull in editor + store machinery.
vi.mock('./ref-utils', () => ({
  StatusBadge: () => null,
  formatFileSize: () => '',
  mediaTypeLabel: (m: string) => m,
  hueForId: () => 0,
  RefIcon: () => null,
}));
vi.mock('../ui', () => ({
  IconButton: (props: { title?: string; onClick?: (e: any) => void; children?: any }) => (
    <button data-title={props.title} onClick={props.onClick}>{props.children}</button>
  ),
  FieldInput: () => null,
  ListPill: (props: { children?: any }) => <div>{props.children}</div>,
  RowActions: (props: { primary?: any; menu?: any }) => <div>{props.primary}{props.menu}</div>,
}));
vi.mock('../../editor/active-editor', () => ({
  useEditorView: () => () => null,
}));
vi.mock('../../hooks/useArmedAction', () => ({
  // Plan reference-archive-v2: track handleClick + arm so the archived (stage-2) delete
  // path can be asserted (single click arms, second click fires the callback).
  useArmedAction: () => {
    let armed = false;
    let pending: (() => void) | null = null;
    return {
      get armed() { return armed; },
      arm: () => { armed = true; },
      disarm: () => { armed = false; pending = null; },
      handleClick: (cb: () => void) => {
        if (armed && pending) { const p = pending; pending = null; armed = false; p(); }
        else { armed = true; pending = cb; }
      },
    };
  },
}));
vi.mock('../../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
vi.mock('../../utils/format', () => ({ formatDate: () => 'Jan 1' }));
// currentUser drives the "is it me" guard for the author-nick segment.
let mockCurrentUserId: string | null = 'me';
vi.mock('../../store/app-store', () => ({
  useAppStore: (selector: (s: { currentUser: { user_id: string } | null }) => unknown) =>
    selector({ currentUser: mockCurrentUserId ? { user_id: mockCurrentUserId } : null }),
}));

function baseRef(over: Partial<Reference>): Reference {
  return {
    reference_id: 'r1',
    project_id: 'p',
    document_id: null,
    title: 'T',
    media_type: 'markdown',
    source_url: null,
    content: 'c',
    processing_status: null,
    file_path: null,
    file_meta: null,
    ...over,
  } as Reference;
}

function renderCard(
  ref: Reference,
  opts: { scopeLabel?: string | null } = {},
) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => {
    root.render(
      <RefCard
        reference={ref}
        isActive={false}
        canEdit={true}
        renamingId={null}
        renameValue=""
        scopeLabel={opts.scopeLabel ?? null}
        onSelect={() => {}}
        onDelete={() => {}}
        onArchive={() => {}}
        onRestore={() => {}}
        onStartRename={() => {}}
        onRenameChange={() => {}}
        onRenameCommit={() => {}}
        onRenameCancel={() => {}}
        onRetry={() => {}}
        onRetryConfirm={() => {}}
        onChangeParent={() => {}}
      />,
    );
  });
  return container;
}

describe('RefCard insert-embed icon', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
  });

  it('renders the insert-embed icon for a markdown (non-image) reference', () => {
    const el = renderCard(baseRef({ media_type: 'markdown' }));
    const btn = el.querySelector('[data-title="insertEmbedAtCursor"]');
    expect(btn).toBeTruthy();
  });

  it('still renders the insert-embed icon for an image reference', () => {
    const el = renderCard(baseRef({ media_type: 'image' }));
    const btn = el.querySelector('[data-title="insertEmbedAtCursor"]');
    expect(btn).toBeTruthy();
  });
});

describe('RefCard staged-delete (archive/restore)', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
  });

  it('live ref: trash is a single-click archive (no arming)', () => {
    const onArchive = vi.fn();
    const onDelete = vi.fn();
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    act(() => {
      root.render(
        <RefCard
          reference={baseRef({})}
          isActive={false} canEdit={true}
          renamingId={null} renameValue="" scopeLabel={null}
          onSelect={() => {}} onDelete={onDelete} onArchive={onArchive} onRestore={() => {}}
          onStartRename={() => {}} onRenameChange={() => {}} onRenameCommit={() => {}}
          onRenameCancel={() => {}} onRetry={() => {}} onRetryConfirm={() => {}}
          onChangeParent={() => {}}
        />,
      );
    });
    const trash = container.querySelector('[data-title="archiveReference"]') as HTMLButtonElement;
    expect(trash).toBeTruthy();
    expect(container.querySelector('[data-title="deleteReferencePermanently"]')).toBeNull();
    act(() => { trash.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(onArchive).toHaveBeenCalledTimes(1);
    expect(onDelete).not.toHaveBeenCalled();
    root.unmount();
  });

  it('archived ref: trash is armed-delete (stage-2, two-click)', () => {
    const onArchive = vi.fn();
    const onDelete = vi.fn();
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    act(() => {
      root.render(
        <RefCard
          reference={baseRef({ archived: true })}
          isActive={false} canEdit={true}
          renamingId={null} renameValue="" scopeLabel={null}
          onSelect={() => {}} onDelete={onDelete} onArchive={onArchive} onRestore={() => {}}
          onStartRename={() => {}} onRenameChange={() => {}} onRenameCommit={() => {}}
          onRenameCancel={() => {}} onRetry={() => {}} onRetryConfirm={() => {}}
          onChangeParent={() => {}}
        />,
      );
    });
    const trash = container.querySelector('[data-title="deleteReferencePermanently"]') as HTMLButtonElement;
    expect(trash).toBeTruthy();
    expect(container.querySelector('[data-title="archiveReference"]')).toBeNull();
    // First click arms (does NOT fire delete); second click fires.
    act(() => { trash.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(onDelete).not.toHaveBeenCalled();
    expect(onArchive).not.toHaveBeenCalled();
    act(() => { trash.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(onDelete).toHaveBeenCalledTimes(1);
    root.unmount();
  });

  it('archived ref: shows the Restore icon (single click → onRestore)', () => {
    const onRestore = vi.fn();
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    act(() => {
      root.render(
        <RefCard
          reference={baseRef({ archived: true })}
          isActive={false} canEdit={true}
          renamingId={null} renameValue="" scopeLabel={null}
          onSelect={() => {}} onDelete={() => {}} onArchive={() => {}} onRestore={onRestore}
          onStartRename={() => {}} onRenameChange={() => {}} onRenameCommit={() => {}}
          onRenameCancel={() => {}} onRetry={() => {}} onRetryConfirm={() => {}}
          onChangeParent={() => {}}
        />,
      );
    });
    const restore = container.querySelector('[data-title="restoreReference"]') as HTMLButtonElement;
    expect(restore).toBeTruthy();
    act(() => { restore.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(onRestore).toHaveBeenCalledTimes(1);
    root.unmount();
  });
});

// ─── Author-nick meta segment ────────────────────────────────────────────────
// The meta row gains a LAST segment — the creator's nickname — joined by ` | `
// in the row's dim typography. Only SOMEONE ELSE's nick shows; own + absent show
// nothing at all (not "You", no placeholder). The last two cases must each have a
// failing branch: a suite testing only the foreign cases passes with the guard missing.
describe('RefCard author-nick meta', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
  });

  // ListRow renders the meta slot inside a span carrying the text-ui-xs dim class.
  function metaText(container: HTMLElement): string {
    return container.querySelector('span[class*="text-ui-xs"]')?.textContent ?? '';
  }

  it('foreign author + parent plate → "date | plate | nick"', () => {
    mockCurrentUserId = 'me';
    const el = renderCard(
      baseRef({ created_by: 'other', created_by_name: 'Bob' }),
      { scopeLabel: 'ParentDoc' },
    );
    expect(metaText(el)).toBe('Jan 1 | ParentDoc | Bob');
  });

  it('foreign author, no plate → "date | nick"', () => {
    mockCurrentUserId = 'me';
    const el = renderCard(
      baseRef({ created_by: 'other', created_by_name: 'Bob', document_id: null }),
      { scopeLabel: null },
    );
    expect(metaText(el)).toBe('Jan 1 | Bob');
  });

  it('own author → no nick segment (plate still shows when present)', () => {
    mockCurrentUserId = 'me';
    const withPlate = renderCard(
      baseRef({ created_by: 'me', created_by_name: 'Me' }),
      { scopeLabel: 'ParentDoc' },
    );
    expect(metaText(withPlate)).toBe('Jan 1 | ParentDoc');
    const noPlate = renderCard(
      baseRef({ created_by: 'me', created_by_name: 'Me', document_id: null }),
      { scopeLabel: null },
    );
    expect(metaText(noPlate)).toBe('Jan 1');
  });

  it('author absent → no nick segment, meta identical to today', () => {
    mockCurrentUserId = 'me';
    const withPlate = renderCard(baseRef({}), { scopeLabel: 'ParentDoc' });
    expect(metaText(withPlate)).toBe('Jan 1 | ParentDoc');
    const noPlate = renderCard(baseRef({ document_id: null }), { scopeLabel: null });
    expect(metaText(noPlate)).toBe('Jan 1');
  });
});

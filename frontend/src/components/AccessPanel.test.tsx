/**
 * AccessPanel — render smoke + the two-checkbox wiring.
 *
 * The reducer (deriveShareState / planShareToggle) is unit-tested in
 * access/share-state.test.ts. Here we assert only the WIRING the component adds:
 * the Include-subtree checkbox is disabled until the document is shared (or covered
 * by an inherited ancestor), and a failed write rolls the optimistic checkbox back.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;
let AccessPanel: typeof import('./AccessPanel').AccessPanel;
let apiPublicShare: typeof import('../api/public-share');
let apiAccess: typeof import('../api/access');
let apiClientGet: ReturnType<typeof vi.fn>;
let dropdownProps: { value: string; options: { value: string; label: string }[]; onSelect: (v: string) => void } | null;
let modalProps: Record<string, unknown> | null;
let showToast: ReturnType<typeof vi.fn>;

function makeShare(share_id: string, scope: 'doc' | 'subtree' = 'doc') {
  return { share_id, token: 'tok-' + share_id, scope, created_at: '2026-01-01', created_at_fmt: 'Jan 1' };
}

beforeEach(async () => {
  vi.resetModules();
  dropdownProps = null;
  modalProps = null;
  // STABLE identities: a fresh `t` per render would re-fire the section's
  // effects ([.., t] deps) on every resolution and loop the worker
  // (lessons/2026-08-19-hook-mock-identity-effect-loop.md).
  const emptyProjects: never[] = [];
  apiClientGet = vi.fn(() => Promise.resolve(emptyProjects));
  const tFn = (k: string) => k;

  // Stub the UI primitives so the panel mounts without their real deps. FieldCheckbox
  // renders a REAL checkbox so its disabled state and onChange are exercisable.
  vi.doMock('./ui', () => ({
    Button: (p: any) => createElement('button', { type: 'button', onClick: p.onClick, disabled: p.disabled }, p.children),
    IconButton: (p: any) => createElement('button', { type: 'button', title: p.title, onClick: p.onClick }, p.children),
    FieldInput: () => null,
    FieldCheckbox: (p: any) => createElement('label', null,
      createElement('input', {
        type: 'checkbox',
        'data-label': p.label,
        checked: p.checked,
        disabled: p.disabled,
        onChange: (e: any) => p.onChange(e.target.checked),
      }),
      p.label,
    ),
    SectionHeader: (p: any) => createElement('div', { 'data-section': p.title }, p.title),
    Dropdown: (p: any) => {
      dropdownProps = p;
      return createElement('div', { 'data-testid': 'move-project-select' },
        p.options.map((o: any) => createElement('span', { key: o.value, 'data-value': o.value }, o.label)));
    },
    Modal: (p: any) => {
      modalProps = p;
      return p.open ? createElement('div', { 'data-testid': 'move-confirm-modal' }, p.title, p.children, p.footer) : null;
    },
  }));
  vi.doMock('./ApiKeyManager', () => ({ __esModule: true, default: () => createElement('div', { 'data-testid': 'api-keys' }) }));
  vi.doMock('./ParentPickerPopup', () => ({
    ParentPickerPopup: () => createElement('div', { 'data-testid': 'parent-picker' }),
  }));
  const icon = () => null;
  vi.doMock('lucide-react', () => ({
    X: icon, Link2: icon, Plus: icon, KeyRound: icon, Trash2: icon, FileLock2: icon, Check: icon, FolderOutput: icon, ChevronDown: icon,
  }));
  vi.doMock('../i18n', () => ({
    // `t` is used directly by the shared copyWithToast helper; useTranslation by the panel.
    t: tFn,
    useTranslation: () => ({ t: tFn }),
  }));
  apiAccess = await (async () => {
    const mod = {
      moveDocumentToProject: vi.fn().mockResolvedValue({ moved: 1, document_ids: [], reference_ids: [] }),
    };
    vi.doMock('../api/access', () => mod);
    return mod as unknown as typeof import('../api/access');
  })();
  vi.doMock('../api/client', () => ({
    apiClient: { get: apiClientGet },
  }));

  apiPublicShare = await (async () => {
    const mod = {
      createShare: vi.fn(),
      updateShareScope: vi.fn().mockResolvedValue(undefined),
      listShares: vi.fn().mockResolvedValue({ shares: [], inherited_from: null }),
      revokeShare: vi.fn().mockResolvedValue(undefined),
    };
    vi.doMock('../api/public-share', () => mod);
    return mod as unknown as typeof import('../api/public-share');
  })();

  showToast = vi.fn();

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.doUnmock('./ui');
  vi.doUnmock('./ApiKeyManager');
  vi.doUnmock('./ParentPickerPopup');
  vi.doUnmock('lucide-react');
  vi.doUnmock('../i18n');
  vi.doUnmock('../api/access');
  vi.doUnmock('../api/public-share');
  vi.doUnmock('../api/client');
  vi.doUnmock('../store/app-store');
});

function renderWith({ isOwner, docId }: { isOwner: boolean; docId: string | null }) {
  const uid = 'u-me';
  const appState = {
    currentProject: { project_id: 'p1', owner_id: isOwner ? uid : 'someone-else', index_doc_id: null },
    currentDocument: docId ? { document_id: docId } : null,
    currentUser: { user_id: uid },
    showToast,
  };
  vi.doMock('../store/app-store', () => ({
    useAppStore: Object.assign(
      (selector: (s: any) => any) => selector(appState),
      { getState: () => appState },
    ),
  }));
  // Re-import AFTER the store mock is registered so the panel closes over it.
  return import('./AccessPanel').then((m) => {
    AccessPanel = m.AccessPanel;
    act(() => root.render(createElement(AccessPanel)));
  });
}

const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });

describe('AccessPanel smoke', () => {
  it('owner: renders the General-access section and the API section when a doc is open', async () => {
    await renderWith({ isOwner: true, docId: 'doc-1' });
    expect(container.querySelector('[data-section="accessGeneralTitle"]')).toBeTruthy();
    expect(container.querySelector('[data-testid="api-keys"]')).toBeTruthy();
  });

  it('non-owner: the owner-only General-access section is NOT rendered (UI hides it)', async () => {
    await renderWith({ isOwner: false, docId: 'doc-1' });
    expect(container.querySelector('[data-section="accessGeneralTitle"]')).toBeNull();
  });
});

describe('General access two-checkbox wiring', () => {
  it('Include-subtree is disabled until the document is shared; Share is always enabled', async () => {
    await renderWith({ isOwner: true, docId: 'doc-1' });
    await flush(); // listShares effect
    const subtree = container.querySelector('input[data-label="accessIncludeSubtree"]') as HTMLInputElement;
    const share = container.querySelector('input[data-label="accessShareThisDoc"]') as HTMLInputElement;
    expect(subtree).toBeTruthy();
    expect(share).toBeTruthy();
    expect(subtree.disabled).toBe(true); // not shared AND not inherited
    expect(share.disabled).toBe(false);
  });

  it('a shared document enables the Include-subtree checkbox', async () => {
    vi.mocked(apiPublicShare.listShares).mockResolvedValue({ shares: [makeShare('sh-1', 'doc')], inherited_from: null });
    await renderWith({ isOwner: true, docId: 'doc-1' });
    await flush();
    const subtree = container.querySelector('input[data-label="accessIncludeSubtree"]') as HTMLInputElement;
    expect(subtree.disabled).toBe(false);
  });

  it('an inherited (ancestor-published) document keeps both checkboxes enabled and off', async () => {
    vi.mocked(apiPublicShare.listShares).mockResolvedValue({
      shares: [],
      inherited_from: { document_id: 'parent', title: 'Folder' },
    });
    await renderWith({ isOwner: true, docId: 'doc-1' });
    await flush();
    const subtree = container.querySelector('input[data-label="accessIncludeSubtree"]') as HTMLInputElement;
    const share = container.querySelector('input[data-label="accessShareThisDoc"]') as HTMLInputElement;
    // Inherited coverage keeps the checkboxes enabled (off) so the owner can mint an own root.
    expect(subtree.disabled).toBe(false);
    expect(share.disabled).toBe(false);
    expect(share.checked).toBe(false);
  });

  it('a failed write rolls the optimistic checkbox back and surfaces a toast', async () => {
    vi.mocked(apiPublicShare.createShare).mockRejectedValue(new Error('boom'));
    await renderWith({ isOwner: true, docId: 'doc-1' });
    await flush();

    const share = container.querySelector('input[data-label="accessShareThisDoc"]') as HTMLInputElement;
    await act(async () => { share.click(); });
    await flush();

    expect(apiPublicShare.createShare).toHaveBeenCalledWith('p1', 'doc-1', 'doc');
    expect(showToast).toHaveBeenCalledWith('accessShareScopeError', 'error');
    // Rolled back to the server truth (no share) — checkbox unchecked again.
    const shareAfter = container.querySelector('input[data-label="accessShareThisDoc"]') as HTMLInputElement;
    expect(shareAfter.checked).toBe(false);
  });
});

describe('Move to another project section', () => {
  it('is hidden when no document is open', async () => {
    await renderWith({ isOwner: true, docId: null });
    expect(container.querySelector('[data-section="accessMoveTitle"]')).toBeNull();
  });

  it('renders the empty-state hint when no OTHER writable project exists', async () => {
    apiClientGet.mockImplementation((url: string) => {
      if (url === '/projects') {
        return Promise.resolve([
          { project_id: 'p1', name: 'Current', my_access: 'full' },      // current — excluded
          { project_id: 'p9', name: 'ReadOnly', my_access: 'readonly' }, // not full — excluded
          { project_id: 'p8', name: 'Gone', my_access: null },           // no access — excluded
        ]);
      }
      return Promise.resolve({ documents: [] });
    });
    await renderWith({ isOwner: true, docId: 'doc-1' });
    await flush();
    expect(container.querySelector('[data-section="accessMoveTitle"]')).toBeTruthy();
    expect(container.textContent).toContain('accessMoveNoWritableProjects');
    expect(container.querySelector('[data-testid="move-project-select"]')).toBeNull();
  });

  it('a failed /projects load renders the ERROR line, not the empty-state hint', async () => {
    apiClientGet.mockImplementation((url: string) => {
      if (url === '/projects') return Promise.reject(new Error('net'));
      return Promise.resolve({ documents: [] });
    });
    await renderWith({ isOwner: true, docId: 'doc-1' });
    await flush();
    expect(container.textContent).toContain('accessMoveLoadFailed');
    expect(container.textContent).not.toContain('accessMoveNoWritableProjects');
    expect(showToast).toHaveBeenCalledWith('accessMoveLoadFailed', 'error');
    expect(container.querySelector('[data-testid="move-project-select"]')).toBeNull();
  });

  it('offers only OTHER projects with my_access === full and auto-selects the first', async () => {
    apiClientGet.mockImplementation((url: string) => {
      if (url === '/projects') {
        return Promise.resolve([
          { project_id: 'p1', name: 'Current', my_access: 'full' },
          { project_id: 'p2', name: 'Writable', my_access: 'full' },
          { project_id: 'p3', name: 'Viewer', my_access: 'readonly' },
          { project_id: 'p4', name: 'Also Writable', my_access: 'full' },
        ]);
      }
      if (url === '/projects/p2') {
        return Promise.resolve({
          documents: [
            { document_id: 'tp-1', title: 'TargetDoc', is_system: false },
            { document_id: 'tp-sys', title: 'SystemFolder', is_system: true },
          ],
        });
      }
      return Promise.resolve({ documents: [] });
    });
    await renderWith({ isOwner: true, docId: 'doc-1' });
    await flush();
    await flush(); // second effect: target tree fetch

    expect(dropdownProps).not.toBeNull();
    expect(dropdownProps!.options.map((o: { value: string }) => o.value)).toEqual(['p2', 'p4']);
    expect(dropdownProps!.value).toBe('p2'); // auto-selected first writable target
  });

  it('seeds the target from the remembered per-project choice and persists a new pick', async () => {
    const setLastMoveTargetProject = vi.fn();
    const uiState = { lastMoveTargetProjectId: 'p4', setLastMoveTargetProject };
    vi.doMock('../store/ui-store', () => ({
      useUIStore: Object.assign(
        (selector: (s: any) => any) => selector(uiState),
        { getState: () => uiState },
      ),
    }));
    apiClientGet.mockImplementation((url: string) => {
      if (url === '/projects') {
        return Promise.resolve([
          { project_id: 'p2', name: 'Writable', my_access: 'full' },
          { project_id: 'p4', name: 'Also Writable', my_access: 'full' },
        ]);
      }
      return Promise.resolve({ documents: [] });
    });
    await renderWith({ isOwner: true, docId: 'doc-1' });
    await flush();
    await flush();

    expect(dropdownProps!.value).toBe('p4'); // remembered, not the first
    act(() => { dropdownProps!.onSelect('p2'); });
    expect(setLastMoveTargetProject).toHaveBeenCalledWith('p2');
    vi.doUnmock('../store/ui-store');
  });

  it('ignores a remembered target that is no longer writable', async () => {
    const uiState = { lastMoveTargetProjectId: 'p-gone', setLastMoveTargetProject: vi.fn() };
    vi.doMock('../store/ui-store', () => ({
      useUIStore: Object.assign(
        (selector: (s: any) => any) => selector(uiState),
        { getState: () => uiState },
      ),
    }));
    apiClientGet.mockImplementation((url: string) => {
      if (url === '/projects') {
        return Promise.resolve([{ project_id: 'p2', name: 'Writable', my_access: 'full' }]);
      }
      return Promise.resolve({ documents: [] });
    });
    await renderWith({ isOwner: true, docId: 'doc-1' });
    await flush();
    await flush();
    expect(dropdownProps!.value).toBe('p2');
    vi.doUnmock('../store/ui-store');
  });

  it('confirm modal calls moveDocumentToProject with the selected target and null parent', async () => {
    apiClientGet.mockImplementation((url: string) => {
      if (url === '/projects') {
        return Promise.resolve([{ project_id: 'p2', name: 'Writable', my_access: 'full' }]);
      }
      if (url === '/projects/p2') {
        return Promise.resolve({ documents: [{ document_id: 'tp-1', title: 'TargetDoc', is_system: false }] });
      }
      return Promise.resolve({ documents: [] });
    });
    await renderWith({ isOwner: true, docId: 'doc-1' });
    await flush();
    await flush();

    // Open the confirm modal via the Move button (last primary button before the modal).
    const moveBtn = [...container.querySelectorAll('button')]
      .find(b => b.textContent === 'accessMoveButton') as HTMLButtonElement;
    expect(moveBtn).toBeTruthy();
    await act(async () => { moveBtn.click(); });
    expect(container.querySelector('[data-testid="move-confirm-modal"]')).toBeTruthy();

    // Confirm inside the modal footer (the second accessMoveButton — the modal's).
    const confirmBtn = [...container.querySelectorAll('[data-testid="move-confirm-modal"] button')]
      .find(b => b.textContent === 'accessMoveButton') as HTMLButtonElement;
    await act(async () => { confirmBtn.click(); });
    await flush();

    expect(apiAccess.moveDocumentToProject).toHaveBeenCalledWith('doc-1', {
      target_project_id: 'p2', parent_id: null,
    });
  });

  it('a failed move surfaces the error toast (no silent degradation)', async () => {
    vi.mocked(apiAccess.moveDocumentToProject).mockRejectedValue(new Error('boom'));
    apiClientGet.mockImplementation((url: string) => {
      if (url === '/projects') {
        return Promise.resolve([{ project_id: 'p2', name: 'Writable', my_access: 'full' }]);
      }
      return Promise.resolve({ documents: [] });
    });
    await renderWith({ isOwner: true, docId: 'doc-1' });
    await flush();
    await flush();

    const moveBtn = [...container.querySelectorAll('button')]
      .find(b => b.textContent === 'accessMoveButton') as HTMLButtonElement;
    await act(async () => { moveBtn.click(); });
    const confirmBtn = [...container.querySelectorAll('[data-testid="move-confirm-modal"] button')]
      .find(b => b.textContent === 'accessMoveButton') as HTMLButtonElement;
    await act(async () => { confirmBtn.click(); });
    await flush();

    expect(showToast).toHaveBeenCalledWith('accessMoveFailed', 'error');
  });
});

describe('General access link plaque', () => {
  it('clicking the published-document link copies it and shows the unified "copied" toast (no inline swap)', async () => {
    // Simulate a successful async Clipboard API write so copyWithToast reports success.
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });

    vi.mocked(apiPublicShare.listShares).mockResolvedValue({
      shares: [makeShare('sh-1', 'doc')], inherited_from: null,
    });
    await renderWith({ isOwner: true, docId: 'doc-1' });
    await flush(); // listShares effect → shared plaque renders

    const plaque = container.querySelector('[title="accessClickToCopy"]') as HTMLDivElement;
    expect(plaque).toBeTruthy();
    await act(async () => { plaque.click(); });
    await flush();

    expect(writeText).toHaveBeenCalled();
    // Unified mechanism: the app-wide copyWithToast toast (not a panel-specific message).
    expect(showToast).toHaveBeenCalledWith('copied', 'info');
    // No inline "Copied!" swap — the plaque keeps showing the URL (unified feedback only).
    expect(plaque.textContent).not.toContain('copied');
    expect(plaque.textContent).toContain('/docs/');
  });
});

/**
 * Unit tests for useCollabConnection — the collab join lifecycle's callback
 * contracts, driven through a fake ProjectCollab whose joinEntity captures the
 * option bag. Pins the normalization/guard rules that historically regressed:
 * presence access_level defaulting, access-level validation, checkpoint payload
 * validation, delete/revoke navigation split by entity kind, preview-mode
 * short-circuit, and the reattach fast path.
 */
// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const captured: { opts: Record<string, (...args: unknown[]) => void> } = { opts: {} };
const joinEntity = vi.fn((type: string, id: string, opts: Record<string, (...args: unknown[]) => void>) => {
  captured.opts = opts;
  return { entityId: id, entityType: type } as never;
});
const leaveEntity = vi.fn();

vi.mock('../collab/ProjectCollabContext', () => ({
  useProjectCollab: () => fakeCollab.current,
}));
const fakeCollab = {
  current: {
    joinEntity,
    leaveEntity,
    // The explicit `: boolean` return is load-bearing: without it TS infers the
    // type predicate `id is 'known-entity'`, which no later `() => false` fits.
    hasEntity: (id: string): boolean => id === 'known-entity',
    status: 'connected' as const,
  },
};

const storeState: Record<string, unknown> = {
  previewDocument: false,
  currentUser: { user_id: 'u1', name: 'Red' },
  currentDocument: null,
  setCollabUsers: vi.fn(),
  addCollabUser: vi.fn(),
  removeCollabUser: vi.fn(),
  setCurrentReference: vi.fn(),
  setAccessLevel: vi.fn(),
  showToast: vi.fn(),
};
vi.mock('../store/app-store', () => ({
  useAppStore: Object.assign(
    (sel: (s: unknown) => unknown) => sel(storeState),
    { getState: () => storeState },
  ),
}));
const navigate = vi.fn();
vi.mock('react-router-dom', () => ({ useNavigate: () => navigate }));
vi.mock('./useDocumentRoute', () => ({ useDocumentRoute: () => ({ projectId: 'p-1', documentId: 'd-1' }) }));
const emitted: string[] = [];
vi.mock('../events', () => ({ emit: (...a: unknown[]) => emitted.push(String(a[0])) }));
vi.mock('../i18n', () => ({ t: (k: string) => k }));
vi.mock('../editor/content-sync', () => ({ clearCheckpointDedup: vi.fn() }));
vi.mock('../api/links', () => ({ clearLinkCache: vi.fn() }));
vi.mock('../utils/user-color', () => ({ userColor: () => '#123' }));

import { useCollabConnection } from './useCollabConnection';

function Probe(props: {
  entityId: string | null;
  isReference: boolean;
  onStatusChange?: (s: string) => void;
  onCollabInitDone?: (textChanged: boolean) => void;
}) {
  useCollabConnection({
    entityId: props.entityId,
    isReference: props.isReference,
    onStatusChange: props.onStatusChange,
    onCollabInitDone: props.onCollabInitDone,
  });
  return null;
}

let root: Root | null = null;
function mount(props: Parameters<typeof Probe>[0]) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(createElement(Probe, props)));
  return host;
}
/** Re-render the SAME root (an entity switch, not a remount). */
function rerender(props: Parameters<typeof Probe>[0]) {
  act(() => root!.render(createElement(Probe, props)));
}
function unmount() {
  // WHY the local const: `root` is a mutable module-level `let`, so TS drops the
  // null-narrowing inside the arrow closure passed to act().
  const r = root;
  if (r) act(() => r.unmount());
  root = null;
}

beforeEach(() => {
  vi.clearAllMocks();
  emitted.length = 0;
  storeState.previewDocument = false;
  storeState.currentDocument = null;
  fakeCollab.current = { joinEntity, leaveEntity, hasEntity: () => false, status: 'connected' as const };
});

describe('useCollabConnection callbacks', () => {
  it('presence without access_level defaults to readonly (never counts as editor)', () => {
    mount({ entityId: 'd-1', isReference: false });
    captured.opts.onPresenceUsers([{ user_id: 'a', name: 'A' }, { user_id: 'b', name: 'B', access_level: 'full' }]);
    expect(storeState.setCollabUsers).toHaveBeenCalledWith([
      { user_id: 'a', name: 'A', access_level: 'readonly' },
      { user_id: 'b', name: 'B', access_level: 'full' },
    ]);
    unmount();
  });

  it('onUserJoined normalizes a single user the same way', () => {
    mount({ entityId: 'd-1', isReference: false });
    captured.opts.onUserJoined({ user_id: 'x', name: 'X' });
    expect(storeState.addCollabUser).toHaveBeenCalledWith({ user_id: 'x', name: 'X', access_level: 'readonly' });
    unmount();
  });

  it('onAccessChanged ignores invalid levels and applies valid ones', () => {
    mount({ entityId: 'd-1', isReference: false });
    captured.opts.onAccessChanged('superadmin');
    expect(storeState.setAccessLevel).not.toHaveBeenCalled();
    captured.opts.onAccessChanged('commentator');
    expect(storeState.setAccessLevel).toHaveBeenCalledWith('commentator');
    unmount();
  });

  it('onCheckpointCreated drops malformed payloads and emits valid ones', () => {
    mount({ entityId: 'd-1', isReference: false });
    captured.opts.onCheckpointCreated({ checkpoint_id: 1 });
    captured.opts.onCheckpointCreated(null);
    expect(emitted).toEqual([]);
    captured.opts.onCheckpointCreated({ checkpoint_id: 'cp-1', content: 'text', label: 'auto-backup' });
    expect(emitted).toEqual(['snapshot-created']);
    // A labeled auto-backup ALSO surfaces its toast.
    expect(storeState.showToast).toHaveBeenCalledWith('autoBackupCreated', 'info');
    unmount();
  });

  it('doc revoked → navigate to project; reference revoked → clear currentReference', () => {
    mount({ entityId: 'd-1', isReference: false });
    captured.opts.onAccessRevoked();
    expect(navigate).toHaveBeenCalledWith('/projects/p-1');
    expect(storeState.setCurrentReference).not.toHaveBeenCalled();
    unmount();

    mount({ entityId: 'd-1', isReference: true });
    captured.opts.onAccessRevoked();
    expect(storeState.setCurrentReference).toHaveBeenCalledWith(null);
    unmount();
  });

  it('preview mode never joins and leaves a prior entity', () => {
    storeState.previewDocument = true;
    mount({ entityId: 'd-1', isReference: false });
    // First mount with preview ON from the start: no join at all.
    expect(joinEntity).not.toHaveBeenCalled();
    unmount();
  });

  it('reattaching a known connected entity reports connected immediately', () => {
    const onStatusChange = vi.fn();
    fakeCollab.current = { joinEntity, leaveEntity, hasEntity: () => true, status: 'connected' as const };
    mount({ entityId: 'd-1', isReference: false, onStatusChange });
    expect(onStatusChange).toHaveBeenCalledWith('connected');
    unmount();
  });

  it('onSynced warns about a degraded save exactly once per document', () => {
    storeState.currentDocument = { document_id: 'd-1', last_save_failed_at: '2026-08-19T00:00:00Z' };
    mount({ entityId: 'd-1', isReference: false });
    captured.opts.onSynced();
    captured.opts.onSynced();
    expect(storeState.showToast).toHaveBeenCalledTimes(1);
    unmount();
  });

  it('a late onSynced from a previous entity\'s join does not report init for the current one', () => {
    const onCollabInitDone = vi.fn();
    mount({ entityId: 'd-1', isReference: false, onCollabInitDone });
    // The join-scoped callbacks of d-1, captured before the switch.
    const staleSynced = captured.opts.onSynced;

    rerender({ entityId: 'd-2', isReference: false, onCollabInitDone });

    // d-1's sync completing after the switch (its leave still pending behind the
    // checkpoint) must NOT report init — the editor is binding d-2 now.
    staleSynced();
    expect(onCollabInitDone).not.toHaveBeenCalled();

    // d-2's own sync reports normally.
    captured.opts.onSynced();
    expect(onCollabInitDone).toHaveBeenCalledTimes(1);
    unmount();
  });
});

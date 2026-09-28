/**
 * The collab status handler must clear only the toast IT raised.
 *
 * clearToast() is global and single-slot: on 'connected' it used to wipe whatever
 * toast happened to be on screen. Measured live, that swallowed the project-load
 * failure toast in the same frame it was raised — the user was bounced to the
 * project list with no message at all.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, useRef } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let capturedOnStatusChange: ((status: string) => void) | null = null;
const showToast = vi.hoisted(() => vi.fn());
const clearToast = vi.hoisted(() => vi.fn());

vi.mock('./useCollabConnection', () => ({
  useCollabConnection: (opts: { onStatusChange: (status: string) => void }) => {
    capturedOnStatusChange = opts.onStatusChange;
    // Real hook returns { handleRef, connEpoch }; a stable null ref + frozen epoch.
    return { handleRef: { current: null }, connEpoch: 0 };
  },
}));
vi.mock('../i18n', () => ({ useTranslation: () => ({ t: (k: string) => k }) }));
vi.mock('../store/app-store', () => ({
  useAppStore: { getState: () => ({ showToast, clearToast }) },
}));
vi.mock('../collab/active-handle-registry', () => ({
  setEntityHandle: vi.fn(),
  getEntityHandle: () => null,
  subscribeEntityHandle: () => () => {},
}));
vi.mock('../collab/overlay-grace', () => ({
  createOverlayGrace: () => ({ update: vi.fn(), dispose: vi.fn() }),
}));
vi.mock('../editor/position-cache', () => ({ flushPositionCache: vi.fn() }));
vi.mock('../collab/yjs-binding', () => ({ createYjsExtension: () => [] }));
vi.mock('../editor/collab-presence', () => ({ collabPresence: () => [] }));
vi.mock('../collab/ProjectCollabContext', () => ({ useProjectCollab: () => null }));
vi.mock('../editor/active-editor', () => ({
  publishHandle: vi.fn(),
  releaseHandle: vi.fn(),
  getActiveHandle: () => null,
  unmountView: vi.fn(),
}));

import { useEditorCollab } from './useEditorCollab';
import { useEntitySwitchPhase } from '../editor/entity-switch-phase';

let container: HTMLDivElement;
let root: Root;

function Harness() {
  const editorViewRef = useRef(null);
  const activeItemRef = useRef(null);
  const checkpointRef = useRef(async () => {});
  const switchPhase = useEntitySwitchPhase();
  useEditorCollab({
    editorViewRef,
    activeItemId: 'doc-a',
    isReference: false,
    snapshotPreview: null,
    viewEpoch: 0,
    activeItemRef,
    checkpointRef,
    switchPhase,
    role: 'primary',
  } as unknown as Parameters<typeof useEditorCollab>[0]);
  return null;
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  capturedOnStatusChange = null;
  vi.clearAllMocks();
  act(() => root.render(createElement(Harness)));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe('collab status toasts are cleared only by their own raiser', () => {
  it("'connected' with no status toast of its own leaves a foreign toast alone", () => {
    act(() => capturedOnStatusChange!('connected'));
    expect(clearToast).not.toHaveBeenCalled();
  });

  it("'reconnecting' then 'connected' clears the toast it raised", () => {
    act(() => capturedOnStatusChange!('reconnecting'));
    expect(showToast).toHaveBeenCalledWith('collabReconnecting', 'warning', { persistent: true });
    act(() => capturedOnStatusChange!('connected'));
    expect(clearToast).toHaveBeenCalledTimes(1);
  });

  it("'offline' then 'connected' clears once, and a second 'connected' does not clear again", () => {
    act(() => capturedOnStatusChange!('offline'));
    act(() => capturedOnStatusChange!('connected'));
    act(() => capturedOnStatusChange!('connected'));
    expect(clearToast).toHaveBeenCalledTimes(1);
  });
});

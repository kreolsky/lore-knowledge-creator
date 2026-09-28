/**
 * Editor — the editable compartment must be applied to EVERY EditorView.
 *
 * Pins the contract behind editor-extension-bundle's data-loss invariant —
 * "a fresh view starts NON-editable; only the editable effect in Editor.tsx
 * opens it". That sentence carries a hidden obligation — the effect must
 * RE-RUN on every EditorView creation, not only when the `editable` VALUE
 * changes. A view can be created while `editable` is already true and stays
 * true (the reference-content loading branch on a doc switch: collab sync
 * completes before hydrateReference lands; a snapshot-preview exit re-mounts
 * the live CM). In that window React sees deps [editable, role] unchanged,
 * skips the effect, and the freshly seeded `editable.of(false)` view is never
 * opened — the editor looks connected and live but is permanently read-only
 * until a hard reload. This is the same defect class the viewEpoch dep
 * already fixed for the yCollab binding (see Editor.tsx viewEpoch comment).
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, useEffect, type FunctionComponent, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { Compartment } from '@codemirror/state';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// The REAL compartment module — Editor.tsx must import the same instance, so
// this module is deliberately NOT mocked. The fake view's dispatches are
// matched against this compartment's reconfigure effect type.
type EditorPluginsModule = typeof import('../editor/editor-plugins');
let editorPlugins: EditorPluginsModule;

let container: HTMLDivElement;
let root: Root;
let Editor: typeof import('./Editor').Editor;

/** Dispatch args recorded from the fake EditorView. */
let dispatches: Array<{ effects?: unknown }>;
/** The fake CM6 view handed to Editor via onCreateView. */
let fakeView: {
  dispatch: (spec: { effects?: unknown }) => void;
  focus: () => void;
  state: { doc: { length: number; toString(): string }; selection: { main: { head: number } } };
  dom: HTMLElement;
};
/** Whether the CM stub has mounted (drives the loading→editor transition). */
let cmMounted: boolean;

/** Collab state the mocked useEditorCollab reports — mutable per phase. */
let collabState: { collabStatus: string; liveForCurrent: boolean };

// Mutable store snapshot — the test mutates it between renders.
const state: Record<string, unknown> = {};

beforeEach(async () => {
  vi.resetModules();
  dispatches = [];
  cmMounted = false;
  collabState = { collabStatus: 'connected', liveForCurrent: true };

  fakeView = {
    dispatch: (spec: { effects?: unknown }) => { dispatches.push(spec); },
    focus: vi.fn(),
    state: {
      doc: { length: 5, toString: () => 'hello' },
      selection: { main: { head: 0 } },
    },
    dom: document.createElement('div'),
  };

  // CM6 wrapper stub: records mount, hands Editor the fake view. The loading
  // branch in Editor.tsx keeps this stub unmounted while content is undefined.
  vi.doMock('./editor/CodeMirrorEditor', () => ({
    __esModule: true,
    default: ({ onCreateView }: { onCreateView?: (v: unknown) => void; children?: ReactNode }) => {
      useEffect(() => {
        cmMounted = true;
        onCreateView?.(fakeView);
      }, []);
      return createElement('div', { 'data-testid': 'cm-stub' });
    },
    cmSetup: () => [],
  }));

  const stub = () => null;
  vi.doMock('./editor/SnapshotPreviewBanner', () => ({ SnapshotPreviewBanner: stub }));
  vi.doMock('./editor/ReferenceViewerBanner', () => ({ ReferenceViewerBanner: stub }));
  vi.doMock('./editor/PreviewDocumentBanner', () => ({ PreviewDocumentBanner: stub }));
  vi.doMock('./editor/CollabPresenceChips', () => ({ CollabPresenceChips: stub }));
  vi.doMock('./editor/ReferenceMediaBar', () => ({ ReferenceMediaBar: stub }));
  vi.doMock('./editor/dev-table-test-hook', () => ({ mountTableTestHook: () => () => {} }));

  vi.doMock('../hooks/useEditorAutosave', () => ({
    useEditorAutosave: () => ({ checkpoint: async () => {} }),
  }));
  vi.doMock('../hooks/useEditorPosition', () => ({ useEditorPosition: () => ({ restoreScrollPosition: () => {} }) }));

  // The unit under test is the [editable] effect vs EditorView creation
  // timing, so the collab hook is driven directly: the machine is LIVE and
  // the status connected from the very first render (the "WS sync won the
  // race against hydrateReference" shape of the doc-switch repro).
  vi.doMock('../hooks/useEditorCollab', () => ({
    useEditorCollab: () => ({
      collabStatus: collabState.collabStatus,
      overlayHidden: true,
      reconnectInfo: { attempts: 0, max: 50 },
      collabConnectionRef: { current: null },
      yjsCompartment: { current: new Compartment() },
      liveForCurrent: collabState.liveForCurrent,
    }),
  }));

  vi.doMock('../i18n', () => ({
    useTranslation: () => ({ t: (k: string) => k }),
    t: (k: string) => k,
  }));
  vi.doMock('../events', () => ({ emit: vi.fn(), on: () => () => {}, off: () => {} }));
  vi.doMock('react-router-dom', () => ({
    useNavigate: () => () => {},
    useParams: () => ({}),
    useLocation: () => ({ pathname: '/' }),
  }));

  state.currentDocument = null;
  state.currentReference = {
    reference_id: 'ref-1',
    document_id: 'doc-1',
    title: 'R',
    media_type: 'text',
    has_content: true,
    content: undefined, // metadata-only until hydrateReference resolves
  };
  state.currentProject = null;
  state.currentUser = null;
  state.previewDocument = null;
  state.snapshotPreview = null;
  state.accessLevel = 'full';
  state.pendingEditorFocus = null;
  state.setPendingEditorFocus = (id: string | null) => { state.pendingEditorFocus = id; };
  state.documents = [];
  state.references = [];
  state.referenceSourceDocId = null;
  state.previewScrollOffset = null;

  const emptyStore = new Proxy(state, {
    get(target: Record<string, unknown>, key: string) {
      if (key in target) return target[key];
      return () => {};
    },
  });
  // NOTE: no `?? fallback` here — a selector legitimately returns null
  // (snapshotPreview), and coalescing would substitute the Proxy.
  const useAppStore = (selector: (s: unknown) => unknown) => selector(emptyStore);
  const useAppStoreRecord = useAppStore as unknown as Record<string, unknown>;
  useAppStoreRecord.subscribe = () => () => {};
  useAppStoreRecord.getState = () => emptyStore;
  useAppStoreRecord.getInitialState = () => emptyStore;
  vi.doMock('../store/app-store', () => ({ useAppStore }));
  vi.doMock('../store/ui-store', () => {
    const uiSnapshot = { documentPositions: {} };
    const useUIStore = (selector?: (s: unknown) => unknown) =>
      selector ? selector(uiSnapshot) : uiSnapshot;
    const record = useUIStore as unknown as Record<string, unknown>;
    record.getState = () => uiSnapshot;
    record.getInitialState = () => uiSnapshot;
    return { useUIStore };
  });

  editorPlugins = await import('../editor/editor-plugins');
  Editor = (await import('./Editor')).Editor;

  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  for (const m of [
    './editor/CodeMirrorEditor', './editor/SnapshotPreviewBanner', './editor/ReferenceViewerBanner',
    './editor/PreviewDocumentBanner', './editor/CollabPresenceChips', './editor/ReferenceMediaBar',
    './editor/dev-table-test-hook', '../hooks/useEditorAutosave',
    '../hooks/useEditorPosition', '../hooks/useEditorCollab', '../i18n', '../events',
    'react-router-dom', '../store/app-store', '../store/ui-store',
  ]) {
    vi.doUnmock(m);
  }
});

/** Flush pending React work (deferred value + epoch-driven re-renders). */
async function flushReact() {
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 0));
  });
}

function editableReconfigureDispatches(): number {
  // The compartment's reconfigure effect type is one instance per compartment,
  // so a freshly created effect's `.type` identifies it across calls.
  const probe = editorPlugins.editableCompartment.reconfigure(true as never);
  const typeOf = (e: unknown) => (e as { type?: unknown } | undefined)?.type;
  let count = 0;
  for (const spec of dispatches) {
    const effects = (spec as { effects?: unknown }).effects;
    if (effects == null) continue;
    const list = Array.isArray(effects) ? effects : [effects];
    if (list.some(e => typeOf(e) === typeOf(probe))) count++;
  }
  return count;
}

describe('Editor editable compartment vs late EditorView creation', () => {
  // Generous timeout: the test dynamically imports the full CM6 module graph
  // (same load cost class as Editor.test.tsx).
  it('applies the current editable value to a view created after collab went live (doc-switch loading branch)', { timeout: 30_000 }, async () => {
    // Phase 1 — reference without content: the loading branch renders, no CM
    // view exists, yet collab is already connected + live (WS sync beat the
    // REST hydrate). editable is TRUE with no view to receive it.
    await act(async () => { root.render(createElement(Editor)); });
    await flushReact();
    expect(cmMounted).toBe(false);

    // Phase 2 — hydrateReference lands: the CM view mounts (key unchanged in
    // value terms — editable stayed true the whole time).
    (state.currentReference as Record<string, unknown>).content = 'hello';
    await act(async () => { root.render(createElement(Editor)); });
    await flushReact();
    expect(cmMounted).toBe(true);

    // Contract: the freshly created view must have the editable compartment
    // reconfigured to the CURRENT value (true) — the seeded false is opened
    // by the effect even though the value never changed across renders.
    expect(editableReconfigureDispatches()).toBeGreaterThan(0);
  });

  // Control: the ordinary doc-switch flow (view mounts while editable is
  // false, then collab goes live and flips it true) must keep dispatching.
  // Guards the fix against "just never dispatch" degenerate solutions.
  it('keeps applying editable on the value flip after an immediate mount (control)', { timeout: 30_000 }, async () => {
    (state.currentReference as Record<string, unknown>).content = 'hello';
    collabState = { collabStatus: 'connecting', liveForCurrent: false };

    await act(async () => { root.render(createElement(Editor)); });
    await flushReact();
    expect(cmMounted).toBe(true);
    const before = editableReconfigureDispatches();
    expect(before).toBeGreaterThan(0); // mount applied editable=false

    collabState = { collabStatus: 'connected', liveForCurrent: true };
    await act(async () => { root.render(createElement(Editor)); });
    await flushReact();
    expect(editableReconfigureDispatches()).toBeGreaterThan(before); // flip applied editable=true
  });
});

// A reference created while quick preview is on renders in the Refs tab's
// SECONDARY editor; the focus request must land there, and only there. The
// mount-time view.focus() does not stick (contenteditable is false during the
// collab join), so the contract is the focus issued when collab goes LIVE.
describe('Editor pending focus after reference create', () => {
  // Editor's props parameter is optional, so createElement would infer `{}`.
  type EditorProps = NonNullable<Parameters<typeof Editor>[0]>;
  const secondary = () => createElement(Editor as FunctionComponent<EditorProps>, {
    entity: state.currentReference as never,
    role: 'secondary',
    hideBanner: true,
  });

  /** Mount while joining, then go live; returns focus calls issued by the flip. */
  async function focusCallsOnGoingLive(): Promise<number> {
    (state.currentReference as Record<string, unknown>).content = 'hello';
    collabState = { collabStatus: 'connecting', liveForCurrent: false };
    await act(async () => { root.render(secondary()); });
    await flushReact();
    const focus = fakeView.focus as ReturnType<typeof vi.fn>;
    const before = focus.mock.calls.length;
    collabState = { collabStatus: 'connected', liveForCurrent: true };
    await act(async () => { root.render(secondary()); });
    await flushReact();
    return focus.mock.calls.length - before;
  }

  it('focuses the secondary editor rendering the requested reference', { timeout: 30_000 }, async () => {
    state.pendingEditorFocus = 'ref-1';
    expect(await focusCallsOnGoingLive()).toBe(1);
    expect(state.pendingEditorFocus).toBeNull();
  });

  it('leaves focus alone when the request names another entity', { timeout: 30_000 }, async () => {
    state.pendingEditorFocus = 'ref-other';
    expect(await focusCallsOnGoingLive()).toBe(0);
    expect(state.pendingEditorFocus).toBe('ref-other');
  });
});

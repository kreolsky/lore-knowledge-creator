/**
 * Contract tests for useEditorCollab — the extracted collab lifecycle hook.
 *
 * Verifies the behaviour-preserving invariants of the collab hook:
 * readiness (machine live) re-enters binding on entity switch; teardown calls
 * checkpoint THEN leave in order; the hook does not mutate its inputs.
 *
 * Minimal renderHook via React.createElement + createRoot (no @testing-library/react),
 * mirroring useResizer.test.ts. Collab dependencies are mocked.
 */

// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createElement, useRef, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

// ── Mocks ────────────────────────────────────────────────────────────────────
// Capture the callbacks useCollabConnection receives so tests can drive them.
let capturedCallbacks: {
  onCollabInitDone: (textChanged: boolean) => void;
  onStatusChange: (status: string) => void;
} | null = null;

// A controllable mock handle whose `leave` records call order against checkpoint.
function makeHandle(leaveFn: () => void) {
  return {
    leave: leaveFn,
    ytext: { toString: () => 'synced' },
    ydoc: {},
    awareness: { getAwareness: () => ({}) },
    reportSelection: vi.fn(),
    synced: true,
  };
}

// Conn-land simulation: the real hook bumps a state epoch on every handle
// assignment/clear; the mock exposes the same via useState so tests can drive
// the "connection arrives AFTER mount" race with bumpConnEpoch().
let bumpConnEpoch: () => void = () => {};

vi.mock('./useCollabConnection', () => ({
  // Real useCollabConnection returns { handleRef, connEpoch }; the ref must be
  // STABLE across renders (the real one returns a useRef).
  useCollabConnection: (opts: { onCollabInitDone: (b: boolean) => void; onStatusChange: (s: string) => void }) => {
    const [epoch, setEpoch] = useState(0);
    bumpConnEpoch = () => setEpoch(e => e + 1);
    capturedCallbacks = { onCollabInitDone: opts.onCollabInitDone, onStatusChange: opts.onStatusChange };
    return { handleRef: stableConnRef, connEpoch: epoch };
  },
}));

// Stable ref shared across renders within a test (reset in beforeEach).
const stableConnRef = { current: null as ReturnType<typeof makeHandle> | null };

vi.mock('../i18n', () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

vi.mock('../store/app-store', () => ({
  useAppStore: { getState: () => ({ showToast: vi.fn(), clearToast: vi.fn() }) },
}));

// Stateful registry mock: the slot and the entity map mirror the real module's
// semantics (identity set/delete + previous-match guards live in the hook).
const registry = vi.hoisted(() => ({
  slot: null as unknown,
  entities: new Map<string, unknown>(),
}));
vi.mock('../collab/active-handle-registry', () => ({
  setEntityHandle: vi.fn((id: string, h: unknown) => {
    if (h === null) registry.entities.delete(id);
    else registry.entities.set(id, h);
  }),
  getEntityHandle: (id: string): unknown => registry.entities.get(id) ?? null,
  subscribeEntityHandle: (): (() => void) => () => {},
}));

vi.mock('../collab/overlay-grace', () => ({
  createOverlayGrace: () => ({ update: vi.fn(), dispose: vi.fn() }),
}));

vi.mock('../editor/position-cache', () => ({
  flushPositionCache: vi.fn(),
}));

vi.mock('../collab/yjs-binding', () => ({
  createYjsExtension: () => [],
}));

vi.mock('../editor/collab-presence', () => ({
  collabPresence: () => [],
}));

vi.mock('../collab/ProjectCollabContext', () => ({
  useProjectCollab: () => null,
}));

// The focused slot mirrors the real module's semantics (releaseHandle carries the
// previous-match guard); unmountView is a spy so tests can assert the teardown's
// unmountView(prevView) call.
vi.mock('../editor/active-editor', () => ({
  publishHandle: vi.fn((h: unknown) => { registry.slot = h; }),
  releaseHandle: vi.fn((prev?: unknown) => {
    if (prev === undefined || registry.slot === prev) registry.slot = null;
  }),
  getActiveHandle: (): unknown => registry.slot,
  unmountView: vi.fn(),
}));

import { useEditorCollab } from './useEditorCollab';
import { setEntityHandle } from '../collab/active-handle-registry';
import { publishHandle, unmountView } from '../editor/active-editor';
import { flushPositionCache } from '../editor/position-cache';
import { useEntitySwitchPhase, type EntitySwitchController } from '../editor/entity-switch-phase';

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  capturedCallbacks = null;
  stableConnRef.current = null;
  lastEditorViewRef = null;
  registry.slot = null;
  registry.entities.clear();
  bumpConnEpoch = () => {};
  vi.clearAllMocks();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

/** Minimal mock EditorView for the yCollab rebind effect: recording dispatch +
 *  the state/scrollSnapshot surface the effect reads. `focused` feeds the slot-claim
 *  guard — NB hasFocus is a GETTER (boolean) on real EditorView, not a method. */
function makeMockView(text = 'synced', focused = false) {
  return {
    dispatch: vi.fn(),
    hasFocus: focused,
    scrollSnapshot: () => ({}),
    state: { doc: { toString: () => text }, selection: { main: { head: 0 } } },
  };
}

// Exposed by the Harness so tests can inject/replace the mock EditorView
// (simulating handleCreateEditor assigning a new view before a viewEpoch bump).
let lastEditorViewRef: { current: unknown } | null = null;

// The phase controller the harness creates and hands to useEditorCollab — the
// same ownership shape Editor.tsx uses (Editor owns, the hook drives).
let lastController: EntitySwitchController | null = null;

/** Machine log as compact strings: `chosen:doc-a` / `bound:doc-a!stale…`. */
function phaseLog(): string[] {
  return (lastController?.log ?? []).map(r =>
    `${r.event.type}${'id' in r.event ? `:${r.event.id}` : ''}${r.accepted ? '' : `!${r.reason ?? 'rejected'}`}`);
}

interface HarnessProps {
  activeItemId: string | undefined;
  viewEpoch: number;
  checkpoint: (item: unknown) => Promise<void>;
  handle: ReturnType<typeof makeHandle> | null;
  /** Column role — the slot-claim guard is primary-only on an empty slot. */
  role?: 'primary' | 'secondary';
}

/**
 * Test harness: holds the inputs in refs/state so tests can re-render with new
 * activeItemId / viewEpoch and observe the hook's behaviour. Also exposes the
 * hook's latest result via a captured ref.
 */
let lastResult: ReturnType<typeof useEditorCollab> | null = null;

function makeHarness() {
  const setPropsRef: { current: ((p: Partial<HarnessProps>) => void) | null } = { current: null };

  function Harness(props: HarnessProps) {
    const [activeItemId, setActiveItemId] = useState(props.activeItemId);
    const [viewEpoch, setViewEpoch] = useState(props.viewEpoch);
    const editorViewRef = useRef(null);
    lastEditorViewRef = editorViewRef;
    const activeItemRef = useRef(null);
    const checkpointRef = useRef(props.checkpoint);
    const switchPhase = useEntitySwitchPhase();
    lastController = switchPhase;

    setPropsRef.current = (p) => {
      if (p.activeItemId !== undefined) setActiveItemId(p.activeItemId);
      if (p.viewEpoch !== undefined) setViewEpoch(p.viewEpoch);
      if (p.checkpoint !== undefined) checkpointRef.current = p.checkpoint;
    };

    const result = useEditorCollab({
      editorViewRef,
      activeItemId,
      isReference: false,
      snapshotPreview: null,
      viewEpoch,
      activeItemRef,
      checkpointRef,
      switchPhase,
      role: props.role ?? 'primary',
    });
    lastResult = result;
    // Inject the mock handle into the connection ref the mocked useCollabConnection returned.
    if (props.handle && result.collabConnectionRef.current === null) {
      ((result.collabConnectionRef as unknown) as { current: unknown }).current = props.handle;
    }
    return null;
  }

  return { Harness, setProps: (p: Partial<HarnessProps>) => setPropsRef.current!(p) };
}

describe('useEditorCollab — contract', () => {
  it('readiness is machine live: init enters live, a switch re-enters binding', () => {
    const leave = vi.fn();
    const handle = makeHandle(leave);
    const checkpoint = vi.fn().mockResolvedValue(undefined);
    const { Harness } = makeHarness();

    act(() => {
      root.render(createElement(Harness, {
        activeItemId: 'doc-a', viewEpoch: 0, checkpoint, handle,
      } as HarnessProps));
    });

    expect(lastController!.state).toEqual({ phase: 'binding', entityId: 'doc-a' });
    expect(capturedCallbacks).not.toBeNull();

    // Simulate collab init for the current entity.
    act(() => capturedCallbacks!.onCollabInitDone(false));
    expect(lastController!.state).toEqual({ phase: 'live', entityId: 'doc-a' });

    // Switch entity → the machine re-enters binding until the new entity's init.
    const { setProps } = makeHarness();
    // Re-render same harness with new id via a fresh mount is awkward; instead verify the
    // reset effect by re-mounting with a different activeItemId.
    act(() => root.unmount());
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => {
      root.render(createElement(Harness, {
        activeItemId: 'doc-b', viewEpoch: 0, checkpoint, handle,
      } as HarnessProps));
    });
    expect(lastController!.state).toEqual({ phase: 'binding', entityId: 'doc-b' });
  });

  it('teardown calls checkpoint THEN leave in order', async () => {
    const order: string[] = [];
    const leave = vi.fn(() => order.push('leave'));
    const handle = makeHandle(leave);
    const checkpoint = vi.fn(() => {
      order.push('checkpoint');
      return Promise.resolve();
    });
    const { Harness } = makeHarness();

    act(() => {
      root.render(createElement(Harness, {
        activeItemId: 'doc-a', viewEpoch: 0, checkpoint, handle,
      } as HarnessProps));
    });

    // Inject a live view so teardown has a previous view to clear.
    const view = makeMockView();
    const viewRef = lastEditorViewRef!;
    viewRef.current = view;

    // Unmount triggers the teardown cleanup.
    act(() => root.unmount());

    // checkpoint runs synchronously in cleanup; leave runs in the .finally microtask.
    expect(checkpoint).toHaveBeenCalledTimes(1);
    expect(flushPositionCache).toHaveBeenCalled();
    // Teardown nulls the view ref and clears the registry with the previous-match guard.
    expect(viewRef.current).toBeNull();
    expect(unmountView).toHaveBeenCalledWith(view);
    // Drain the microtask so the .finally (leave) runs.
    await Promise.resolve();
    await Promise.resolve();
    expect(leave).toHaveBeenCalledTimes(1);
    expect(order).toEqual(['checkpoint', 'leave']);
  });

  it('re-attach wins over reset: switch re-enters binding, then init flips it live', () => {
    const handle = makeHandle(vi.fn());
    const checkpoint = vi.fn().mockResolvedValue(undefined);
    const { Harness, setProps } = makeHarness();

    act(() => {
      root.render(createElement(Harness, {
        activeItemId: 'doc-a', viewEpoch: 0, checkpoint, handle,
      } as HarnessProps));
    });
    act(() => capturedCallbacks!.onCollabInitDone(false));
    expect(lastController!.state).toEqual({ phase: 'live', entityId: 'doc-a' });

    // In-place entity switch A→B: chosen(B) applies in the same flush…
    act(() => setProps({ activeItemId: 'doc-b' }));
    expect(lastController!.state).toEqual({ phase: 'binding', entityId: 'doc-b' });

    // …then the connection's synthesized onCollabInitDone (re-attach) is attributed
    // to B and wins. If chosen ever registered after the join, bound would be
    // rejected and the phase would stay binding.
    act(() => capturedCallbacks!.onCollabInitDone(false));
    expect(lastController!.state).toEqual({ phase: 'live', entityId: 'doc-b' });
  });

  it('viewEpoch bump rebinds yCollab against the CURRENT view ref', () => {
    const handle = makeHandle(vi.fn());
    const checkpoint = vi.fn().mockResolvedValue(undefined);
    const { Harness, setProps } = makeHarness();

    act(() => {
      root.render(createElement(Harness, {
        activeItemId: 'doc-a', viewEpoch: 0, checkpoint, handle,
      } as HarnessProps));
    });
    const firstView = makeMockView();
    lastEditorViewRef!.current = firstView;

    // Init → the binding effect runs against the first view.
    act(() => capturedCallbacks!.onCollabInitDone(false));
    expect(firstView.dispatch).toHaveBeenCalledTimes(1);

    // Simulate handleCreateEditor: a NEW view is created (StrictMode remount /
    // snapshot-preview exit) without activeItemId/live-phase/snapshotPreview changing.
    const secondView = makeMockView();
    lastEditorViewRef!.current = secondView;
    act(() => setProps({ viewEpoch: 1 }));

    // The rebind must target the CURRENT ref value — not the stale first view.
    expect(secondView.dispatch).toHaveBeenCalledTimes(1);
    expect(firstView.dispatch).toHaveBeenCalledTimes(1);
  });

  it('does not mutate the props it receives (read-only viewEpoch dep)', () => {
    const leave = vi.fn();
    const handle = makeHandle(leave);
    const checkpoint = vi.fn().mockResolvedValue(undefined);
    const { Harness, setProps } = makeHarness();

    act(() => {
      root.render(createElement(Harness, {
        activeItemId: 'doc-a', viewEpoch: 0, checkpoint, handle,
      } as HarnessProps));
    });

    const before = lastResult!.collabConnectionRef;

    // Bump viewEpoch — yCollab rebind effect re-runs (no view → early return, no throw).
    act(() => setProps({ viewEpoch: 1 }));
    act(() => setProps({ viewEpoch: 2 }));

    // The hook must not throw, and the connection ref identity is stable (no re-create).
    expect(lastResult).not.toBeNull();
    expect(lastResult!.collabConnectionRef).toBe(before);
    expect(lastResult!.collabStatus).toBe('connecting');
  });
});

describe('useEditorCollab — phase machine drivers', () => {
  it('chosen at reset-effect setup, bound on init, teardown+chosen on switch, teardown on unmount', () => {
    const handle = makeHandle(vi.fn());
    const checkpoint = vi.fn().mockResolvedValue(undefined);
    const { Harness, setProps } = makeHarness();

    act(() => {
      root.render(createElement(Harness, {
        activeItemId: 'doc-a', viewEpoch: 0, checkpoint, handle,
      } as HarnessProps));
    });
    // Machine enters binding BEFORE the join runs (reset effect registers first).
    expect(phaseLog()).toEqual(['chosen:doc-a']);
    expect(lastController!.state).toEqual({ phase: 'binding', entityId: 'doc-a' });

    // Collab init for the current entity → live.
    act(() => capturedCallbacks!.onCollabInitDone(false));
    expect(lastController!.state).toEqual({ phase: 'live', entityId: 'doc-a' });

    // Entity switch: teardown(prev) in the cleanup pass, chosen(next) in setup.
    // The machine never shows a live phase for the wrong entity.
    act(() => setProps({ activeItemId: 'doc-b' }));
    expect(phaseLog().slice(-2)).toEqual(['teardown:doc-a', 'chosen:doc-b']);
    expect(lastController!.state).toEqual({ phase: 'binding', entityId: 'doc-b' });

    // Re-attach init for the new entity lands on the binding phase → accepted.
    act(() => capturedCallbacks!.onCollabInitDone(false));
    expect(lastController!.state).toEqual({ phase: 'live', entityId: 'doc-b' });

    // Unmount records the final teardown.
    act(() => root.unmount());
    expect(phaseLog().slice(-1)).toEqual(['teardown:doc-b']);
  });

  it('a bound the grammar rejects leaves the phase untouched (duplicate init)', () => {
    const handle = makeHandle(vi.fn());
    const checkpoint = vi.fn().mockResolvedValue(undefined);
    const { Harness } = makeHarness();

    act(() => {
      root.render(createElement(Harness, {
        activeItemId: 'doc-a', viewEpoch: 0, checkpoint, handle,
      } as HarnessProps));
    });
    act(() => capturedCallbacks!.onCollabInitDone(false));
    expect(lastController!.state).toEqual({ phase: 'live', entityId: 'doc-a' });

    // A duplicate init (yjs onSynced firing twice) is rejected by the grammar and
    // must not move the phase.
    act(() => capturedCallbacks!.onCollabInitDone(false));
    const last = (phaseLog().slice(-1))[0];
    expect(last).toContain('bound:doc-a!');
    expect(lastController!.state).toEqual({ phase: 'live', entityId: 'doc-a' });
  });
});

describe('useEditorCollab — entity registry publish + guarded slot claim', () => {
  /** Fresh root after a mid-test unmount (mirrors the pattern in the contract suite).
   *  Nulls the shared conn ref and the registry state: a REAL new Editor mounts with
   *  its own null-initialized handleRef into the registry state its teardown left —
   *  the mock's shared ref/slot must not leak the previous block's handle into the
   *  new mount's publish. */
  function freshRoot() {
    stableConnRef.current = null;
    registry.slot = null;
    registry.entities.clear();
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  }

  it('conn lands after mount: focused column republishes the slot AND registers the entity map', () => {
    const handle = makeHandle(vi.fn());
    const checkpoint = vi.fn().mockResolvedValue(undefined);
    const { Harness } = makeHarness();

    // Mount BEFORE the connection exists (projectCollab arrives post-mount).
    act(() => {
      root.render(createElement(Harness, {
        activeItemId: 'doc-a', viewEpoch: 0, checkpoint, handle: null,
      } as HarnessProps));
    });
    expect(publishHandle).not.toHaveBeenCalledWith(handle);
    expect(setEntityHandle).not.toHaveBeenCalledWith('doc-a', handle);

    // The view exists and holds focus; the connection lands late.
    lastEditorViewRef!.current = makeMockView('synced', true);
    stableConnRef.current = handle;
    act(() => bumpConnEpoch());

    expect(publishHandle).toHaveBeenCalledWith(handle);
    expect(setEntityHandle).toHaveBeenCalledWith('doc-a', handle);
    expect(registry.slot).toBe(handle);
  });

  it('secondary with null conn does not clobber a non-null slot; its entity handle still registers', () => {
    const foreign = makeHandle(vi.fn());
    const handle = makeHandle(vi.fn());
    const checkpoint = vi.fn().mockResolvedValue(undefined);
    const { Harness } = makeHarness();

    // The slot already holds the OTHER column's handle.
    registry.slot = foreign;

    act(() => {
      root.render(createElement(Harness, {
        activeItemId: 'ref-b', viewEpoch: 0, checkpoint, handle: null, role: 'secondary',
      } as HarnessProps));
    });
    lastEditorViewRef!.current = makeMockView('synced', false);
    stableConnRef.current = handle;
    act(() => bumpConnEpoch());

    // Unfocused secondary: never touches the slot…
    expect(registry.slot).toBe(foreign);
    expect(publishHandle).not.toHaveBeenCalledWith(handle);
    // …but its OWN entity still lands in the entity map (identity, not focus).
    expect(setEntityHandle).toHaveBeenCalledWith('ref-b', handle);
  });

  it('unfocused secondary does NOT claim a null slot; the primary may (single-column first paint)', () => {
    const checkpoint = vi.fn().mockResolvedValue(undefined);

    // Secondary, unfocused, empty slot → must not claim.
    {
      const { Harness } = makeHarness();
      const handle = makeHandle(vi.fn());
      act(() => {
        root.render(createElement(Harness, {
          activeItemId: 'ref-b', viewEpoch: 0, checkpoint, handle: null, role: 'secondary',
        } as HarnessProps));
        });
      lastEditorViewRef!.current = makeMockView('synced', false);
      stableConnRef.current = handle;
      act(() => bumpConnEpoch());
      expect(publishHandle).not.toHaveBeenCalledWith(handle);
      expect(registry.slot).toBeNull();
      act(() => root.unmount());
    }

    // Primary, unfocused, empty slot → claims (single-column mode IS primary).
    {
      const { Harness } = makeHarness();
      freshRoot();
      const handle = makeHandle(vi.fn());
      act(() => {
        root.render(createElement(Harness, {
          activeItemId: 'doc-a', viewEpoch: 0, checkpoint, handle: null, role: 'primary',
        } as HarnessProps));
      });
      lastEditorViewRef!.current = makeMockView('synced', false);
      stableConnRef.current = handle;
      act(() => bumpConnEpoch());
      expect(publishHandle).toHaveBeenCalledWith(handle);
      expect(registry.slot).toBe(handle);
    }
  });

  it('teardown nulls only its own handle (slot AND entity map)', () => {
    const checkpoint = vi.fn().mockResolvedValue(undefined);

    // Mount focused primary that claims the slot with handleA.
    {
      const handleA = makeHandle(vi.fn());
      const { Harness } = makeHarness();
      act(() => {
        root.render(createElement(Harness, {
          activeItemId: 'doc-a', viewEpoch: 0, checkpoint, handle: handleA,
        } as HarnessProps));
      });
      lastEditorViewRef!.current = makeMockView('synced', true);
      stableConnRef.current = handleA;
      act(() => bumpConnEpoch());
      expect(registry.slot).toBe(handleA);
      expect(registry.entities.get('doc-a')).toBe(handleA);

      // Another column takes the slot via focus while this one blurs.
      const foreign = makeHandle(vi.fn());
      registry.slot = foreign;

      act(() => root.unmount());

      // Guarded teardown: the FOREIGN slot survives; OUR entity entry is cleared.
      expect(registry.slot).toBe(foreign);
      expect(setEntityHandle).toHaveBeenCalledWith('doc-a', null);
      expect(registry.entities.has('doc-a')).toBe(false);
    }

    // Counterpart: when the slot still holds OUR handle, teardown nulls both.
    {
      const { Harness } = makeHarness();
      freshRoot();
      const handleB = makeHandle(vi.fn());
      act(() => {
        root.render(createElement(Harness, {
          activeItemId: 'doc-b', viewEpoch: 0, checkpoint, handle: handleB,
        } as HarnessProps));
      });
      lastEditorViewRef!.current = makeMockView('synced', false);
      stableConnRef.current = handleB;
      act(() => bumpConnEpoch());
      expect(registry.slot).toBe(handleB);

      act(() => root.unmount());

      expect(registry.slot).toBeNull();
      expect(setEntityHandle).toHaveBeenCalledWith('doc-b', null);
      expect(registry.entities.has('doc-b')).toBe(false);
    }
  });

  it('conn drops to null for the SAME entity (preview leave): releases our slot claim and entity entry', () => {
    const checkpoint = vi.fn().mockResolvedValue(undefined);

    // Focused primary that owns the slot and the entity entry.
    {
      const { Harness } = makeHarness();
      const handle = makeHandle(vi.fn());
      act(() => {
        root.render(createElement(Harness, {
          activeItemId: 'doc-a', viewEpoch: 0, checkpoint, handle,
        } as HarnessProps));
      });
      lastEditorViewRef!.current = makeMockView('synced', true);
      stableConnRef.current = handle;
      act(() => bumpConnEpoch());
      expect(registry.slot).toBe(handle);
      expect(registry.entities.get('doc-a')).toBe(handle);

      // The connection leaves WITHOUT an entity switch (preview-mode leave):
      // handleRef nulled, epoch bumped. Re-render WITHOUT the handle — the harness
      // re-injects props.handle into the null ref on every render, which would
      // simulate an instant reconnect instead of the leave.
      stableConnRef.current = null;
      act(() => {
        root.render(createElement(Harness, {
          activeItemId: 'doc-a', viewEpoch: 0, checkpoint, handle: null,
        } as HarnessProps));
        bumpConnEpoch();
      });

      // Our publication is released — no entry holds the LEFT ydoc.
      expect(registry.slot).toBeNull();
      expect(registry.entities.has('doc-a')).toBe(false);
      act(() => root.unmount());
    }

    // Same drop, but the slot was already taken over by a FOREIGN handle.
    {
      const { Harness } = makeHarness();
      freshRoot();
      const foreign = makeHandle(vi.fn());
      const handle = makeHandle(vi.fn());
      act(() => {
        root.render(createElement(Harness, {
          activeItemId: 'doc-a', viewEpoch: 0, checkpoint, handle,
        } as HarnessProps));
      });
      lastEditorViewRef!.current = makeMockView('synced', false);
      stableConnRef.current = handle;
      act(() => bumpConnEpoch());
      registry.slot = foreign; // focus moved to the other column meanwhile

      stableConnRef.current = null;
      act(() => {
        root.render(createElement(Harness, {
          activeItemId: 'doc-a', viewEpoch: 0, checkpoint, handle: null,
        } as HarnessProps));
        bumpConnEpoch();
      });

      // Guarded release: the FOREIGN slot survives; OUR entity entry still goes.
      expect(registry.slot).toBe(foreign);
      expect(registry.entities.has('doc-a')).toBe(false);
      act(() => root.unmount());
    }
  });
});

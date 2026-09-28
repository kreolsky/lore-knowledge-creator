/**
 * Entity-switch phase machine — transition contract + controller tests.
 *
 * The machine makes the Editor entity switch explicit (idle → leaving → binding →
 * live) as one transition sequence, replacing ~20 effects that each react to
 * activeItemId independently. Two tiers:
 *   1. Pure `transition()` — the legal event grammar, including the stale-init
 *      rejection that guards the re-attach race (init done for a previous entity
 *      must never mark the current one live).
 *   2. Controller — `apply()` applies synchronously, rejected events leave state
 *      untouched, and the ref-backed log records through unmount (where a state
 *      updater would be dropped). The real drivers live in useEditorCollab's
 *      effects and are covered by its test file.
 *
 * Minimal renderHook via React.createElement + createRoot (no @testing-library/react),
 * mirroring useEditorCollab.test.ts.
 */
// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import {
  initialSwitchState,
  transition,
  isLiveFor,
  useEntitySwitchPhase,
  type SwitchState,
  type SwitchRecord,
  type EntitySwitchController,
} from './entity-switch-phase';

(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// ── Pure transition contract ─────────────────────────────────────────────────

describe('entity-switch transition — legal walk', () => {
  it('idle → binding → live on first entity', () => {
    let s = initialSwitchState();
    expect(s).toEqual({ phase: 'idle', entityId: null });
    let r = transition(s, { type: 'chosen', id: 'doc-a' });
    expect(r.accepted).toBe(true);
    expect(r.next).toEqual({ phase: 'binding', entityId: 'doc-a' });
    s = r.next;
    r = transition(s, { type: 'bound', id: 'doc-a' });
    expect(r.accepted).toBe(true);
    expect(r.next).toEqual({ phase: 'live', entityId: 'doc-a' });
  });

  it('live → leaving → binding → live on entity switch', () => {
    let s: SwitchState = { phase: 'live', entityId: 'doc-a' };
    let r = transition(s, { type: 'teardown', id: 'doc-a' });
    expect(r.accepted).toBe(true);
    expect(r.next).toEqual({ phase: 'leaving', entityId: 'doc-a' });
    s = r.next;
    r = transition(s, { type: 'chosen', id: 'ref-b' });
    expect(r.accepted).toBe(true);
    expect(r.next).toEqual({ phase: 'binding', entityId: 'ref-b' });
    s = r.next;
    r = transition(s, { type: 'bound', id: 'ref-b' });
    expect(r.accepted).toBe(true);
    expect(r.next).toEqual({ phase: 'live', entityId: 'ref-b' });
  });

  it('binding (never bound) entity can be torn down directly', () => {
    let s: SwitchState = { phase: 'binding', entityId: 'doc-a' };
    const r = transition(s, { type: 'teardown', id: 'doc-a' });
    expect(r.accepted).toBe(true);
    expect(r.next).toEqual({ phase: 'leaving', entityId: 'doc-a' });
  });

  it('leaving → idle when the switch target is no entity (cleared)', () => {
    let s: SwitchState = { phase: 'leaving', entityId: 'doc-a' };
    const r = transition(s, { type: 'cleared' });
    expect(r.accepted).toBe(true);
    expect(r.next).toEqual({ phase: 'idle', entityId: null });
  });

  it('live → binding on collab loss, back to live on re-init (same entity)', () => {
    let s: SwitchState = { phase: 'live', entityId: 'doc-a' };
    let r = transition(s, { type: 'lost' });
    expect(r.accepted).toBe(true);
    expect(r.next).toEqual({ phase: 'binding', entityId: 'doc-a' });
    s = r.next;
    r = transition(s, { type: 'bound', id: 'doc-a' });
    expect(r.accepted).toBe(true);
    expect(r.next).toEqual({ phase: 'live', entityId: 'doc-a' });
  });

  it('StrictMode remount walk: chosen → teardown → chosen again is legal', () => {
    let s = initialSwitchState();
    s = transition(s, { type: 'chosen', id: 'doc-a' }).next;
    s = transition(s, { type: 'teardown', id: 'doc-a' }).next;
    const r = transition(s, { type: 'chosen', id: 'doc-a' });
    expect(r.accepted).toBe(true);
    expect(r.next).toEqual({ phase: 'binding', entityId: 'doc-a' });
  });
});

describe('entity-switch transition — rejections (the races this machine exists for)', () => {
  it('rejects a stale init: bound(prev) while binding(next)', () => {
    const s: SwitchState = { phase: 'binding', entityId: 'ref-b' };
    const r = transition(s, { type: 'bound', id: 'doc-a' });
    expect(r.accepted).toBe(false);
    expect(r.reason).toContain('stale');
    // Rejected transitions must not move the state.
    expect(r.next).toBe(s);
  });

  it('rejects a duplicate bound while already live', () => {
    const s: SwitchState = { phase: 'live', entityId: 'doc-a' };
    const r = transition(s, { type: 'bound', id: 'doc-a' });
    expect(r.accepted).toBe(false);
    expect(r.next).toBe(s);
  });

  it('rejects teardown for a non-current entity', () => {
    const s: SwitchState = { phase: 'live', entityId: 'doc-a' };
    const r = transition(s, { type: 'teardown', id: 'ref-b' });
    expect(r.accepted).toBe(false);
    expect(r.next).toBe(s);
  });

  it('rejects chosen without a preceding teardown (switch skipped its cleanup)', () => {
    const s: SwitchState = { phase: 'live', entityId: 'doc-a' };
    const r = transition(s, { type: 'chosen', id: 'ref-b' });
    expect(r.accepted).toBe(false);
    expect(r.next).toBe(s);
  });

  it('rejects teardown/lost from idle', () => {
    const s = initialSwitchState();
    expect(transition(s, { type: 'teardown', id: 'doc-a' }).accepted).toBe(false);
    expect(transition(s, { type: 'lost' }).accepted).toBe(false);
  });
});

// ── Controller: synchronous apply + surviving log ─────────────────────────────

let container: HTMLDivElement;
let root: Root;
let last: EntitySwitchController | null = null;

function Harness() {
  last = useEntitySwitchPhase();
  return null;
}

/** Log as compact strings: `chosen:doc-a+` (accepted) / `bound:doc-a!stale…` (rejected). */
function logLines(): string[] {
  return (last?.log ?? []).map(r =>
    `${r.event.type}${'id' in r.event ? `:${r.event.id}` : ''}${r.accepted ? '' : `!${r.reason ?? 'rejected'}`}`);
}

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  last = null;
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe('useEntitySwitchPhase — controller', () => {
  it('apply() walks the grammar synchronously: state is post-transition immediately', () => {
    act(() => root.render(createElement(Harness)));
    act(() => last!.apply({ type: 'chosen', id: 'doc-a' }));
    expect(last!.state).toEqual({ phase: 'binding', entityId: 'doc-a' });
    act(() => last!.apply({ type: 'bound', id: 'doc-a' }));
    expect(last!.state).toEqual({ phase: 'live', entityId: 'doc-a' });
    expect(logLines()).toEqual(['chosen:doc-a', 'bound:doc-a']);
  });

  it('rejected apply() returns false and leaves state + a reason in the log', () => {
    act(() => root.render(createElement(Harness)));
    act(() => last!.apply({ type: 'chosen', id: 'doc-a' }));
    expect(last!.apply({ type: 'bound', id: 'doc-old' })).toBe(false);
    expect(last!.state).toEqual({ phase: 'binding', entityId: 'doc-a' });
    expect(logLines()[1]).toContain('stale');
  });

  it('records through unmount — the final teardown is never dropped', () => {
    act(() => root.render(createElement(Harness)));
    act(() => last!.apply({ type: 'chosen', id: 'doc-a' }));
    act(() => root.unmount());
    // The real driver calls apply inside the unmount cleanup; the closure and its
    // ref-backed log must still work there (a state updater would be dropped).
    expect(() => last!.apply({ type: 'teardown', id: 'doc-a' })).not.toThrow();
    expect(logLines().slice(-1)).toEqual(['teardown:doc-a']);
    expect(last!.state).toEqual({ phase: 'leaving', entityId: 'doc-a' });
  });
});

// ── isLiveFor: the one derivation both consumers read ─────────────────────────

describe('isLiveFor — live for the entity being rendered', () => {
  it('is true only in the live phase AND for the current entity', () => {
    expect(isLiveFor({ phase: 'live', entityId: 'doc-a' }, 'doc-a')).toBe(true);
    expect(isLiveFor({ phase: 'live', entityId: 'doc-a' }, 'doc-b')).toBe(false);
    expect(isLiveFor({ phase: 'binding', entityId: 'doc-a' }, 'doc-a')).toBe(false);
    expect(isLiveFor({ phase: 'leaving', entityId: 'doc-a' }, 'doc-a')).toBe(false);
    expect(isLiveFor(initialSwitchState(), undefined)).toBe(false);
  });

  it('is false for an undefined entity even while the machine is live', () => {
    // Why: `activeItemId` is undefined between entities; a nullish==nullish match
    // would mark the editor editable against the entity it is leaving.
    expect(isLiveFor({ phase: 'live', entityId: 'doc-a' }, undefined)).toBe(false);
    expect(isLiveFor({ phase: 'live', entityId: null }, undefined)).toBe(false);
  });

  it('controller exposes it against its own current state', () => {
    act(() => root.render(createElement(Harness)));
    act(() => last!.apply({ type: 'chosen', id: 'doc-a' }));
    expect(last!.isLiveFor('doc-a')).toBe(false);
    act(() => last!.apply({ type: 'bound', id: 'doc-a' }));
    expect(last!.isLiveFor('doc-a')).toBe(true);
    expect(last!.isLiveFor('doc-b')).toBe(false);
    act(() => last!.apply({ type: 'lost' }));
    expect(last!.isLiveFor('doc-a')).toBe(false);
  });
});

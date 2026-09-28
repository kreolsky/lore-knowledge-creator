/**
 * Editor entity-switch phase machine: idle → leaving → binding → live.
 * The switch lifecycle Editor.tsx owns, as one explicit transition sequence
 * instead of ~20 effects each reacting to a deferred activeItemId in
 * registration order. Editor.tsx creates the controller and hands it to
 * useEditorCollab, whose effects DRIVE it: the reset effect emits
 * chosen/cleared (+ teardown in its cleanup), the init callback emits bound
 * (gated — a rejected bound never raises readiness), the status handler emits
 * lost. The transition grammar rejects the historical race shapes (stale init,
 * teardown for the wrong entity, a chosen that skipped its teardown).
 *
 * 'leaving' is transient and never rendered: React runs the cleanup pass
 * (teardown) and the setup pass (chosen) inside one flush, so no render observes
 * 'leaving' — an extra frame per switch is the flicker class this machine exists
 * to prevent. The transition LOG is the assertion surface for the ordering
 * contract (unit tests + dev diagnostics).
 */

import { useRef, useReducer, useCallback, useMemo } from 'react';

export type EditorSwitchPhase = 'idle' | 'leaving' | 'binding' | 'live';

export interface SwitchState {
  phase: EditorSwitchPhase;
  entityId: string | null;
}

export type SwitchEvent =
  | { type: 'teardown'; id: string }
  | { type: 'chosen'; id: string }
  | { type: 'cleared' }
  | { type: 'bound'; id: string }
  | { type: 'lost' };

export interface SwitchRecord {
  event: SwitchEvent;
  accepted: boolean;
  reason?: string;
  phase: EditorSwitchPhase;
}

export function initialSwitchState(): SwitchState {
  return { phase: 'idle', entityId: null };
}

/**
 * The one derivation of "the machine is live for the entity being rendered".
 *
 * INVARIANT: live requires BOTH the live phase and an id match against a
 * non-nullish entity. Why: `activeItemId` is undefined between entities, and a
 * nullish==nullish match would report live for the entity the editor is leaving
 * — the stale-render race this machine exists to remove. Both consumers (the
 * editable gate in Editor.tsx, the yCollab binding + overlay grace in
 * useEditorCollab) read THIS function; a re-derivation at a call site is how the
 * two drifted apart before.
 */
export function isLiveFor(state: SwitchState, entityId: string | undefined | null): boolean {
  if (!entityId) return false;
  return state.phase === 'live' && state.entityId === entityId;
}

interface TransitionResult {
  next: SwitchState;
  accepted: boolean;
  reason?: string;
}

function reject(state: SwitchState, reason: string): TransitionResult {
  return { next: state, accepted: false, reason };
}

export function transition(state: SwitchState, event: SwitchEvent): TransitionResult {
  switch (event.type) {
    case 'teardown':
      if (state.entityId !== event.id) {
        return reject(state, `teardown for ${event.id} while current entity is ${state.entityId}`);
      }
      if (state.phase === 'idle') {
        return reject(state, 'teardown with no entity');
      }
      return { next: { phase: 'leaving', entityId: event.id }, accepted: true };
    case 'chosen':
      // INVARIANT: an entity may be chosen only from idle/leaving — chosen while
      // binding/live means a switch setup ran without its cleanup.
      // Why: React guarantees cleanup-before-setup per effect pair; anything else
      // is a feed bug that would silently re-order the entity switch.
      if (state.phase === 'binding' || state.phase === 'live') {
        return reject(state, `chosen ${event.id} from ${state.phase} without a preceding teardown`);
      }
      return { next: { phase: 'binding', entityId: event.id }, accepted: true };
    case 'cleared':
      return { next: initialSwitchState(), accepted: true };
    case 'bound':
      // INVARIANT(corruption): init is accepted only for the CURRENT binding entity.
      // Why: a stale init for the previous entity arriving after the switch is the
      // re-attach race that historically bound yCollab/editable against the wrong
      // doc; accepting it here would re-introduce that defect.
      if (state.entityId !== event.id) {
        return reject(state, `stale init for ${event.id} while binding ${state.entityId}`);
      }
      if (state.phase !== 'binding') {
        return reject(state, `bound while ${state.phase}`);
      }
      return { next: { phase: 'live', entityId: event.id }, accepted: true };
    case 'lost':
      if (state.phase === 'live') {
        return { next: { phase: 'binding', entityId: state.entityId }, accepted: true };
      }
      if (state.phase === 'binding') {
        return { next: state, accepted: true };
      }
      return reject(state, `lost while ${state.phase}`);
  }
}

export interface EntitySwitchController {
  readonly state: SwitchState;
  readonly log: SwitchRecord[];
  /** Apply one transition; false = rejected (state unchanged, reason in the log). */
  apply(event: SwitchEvent): boolean;
  /** `isLiveFor(state, entityId)` bound to this controller's current state. */
  isLiveFor(entityId: string | undefined | null): boolean;
}

/**
 * The controller for one Editor instance's switch lifecycle. Editor.tsx owns the
 * instance and passes it to useEditorCollab, which drives it from its effects.
 */
export function useEntitySwitchPhase(): EntitySwitchController {
  const stateRef = useRef<SwitchState>(initialSwitchState());
  const logRef = useRef<SwitchRecord[]>([]);
  // WHY useReducer force instead of useState: transitions must apply
  // SYNCHRONOUSLY (the log/ref must already show teardown→chosen when the paired
  // setup runs in the same flush) and must keep recording through the unmount
  // teardown, where a useState updater would be silently dropped.
  const [, forceRender] = useReducer((c: number) => c + 1, 0);

  const apply = useCallback((event: SwitchEvent): boolean => {
    const { next, accepted, reason } = transition(stateRef.current, event);
    logRef.current.push({ event, accepted, reason, phase: next.phase });
    if (accepted) stateRef.current = next;
    forceRender();
    return accepted;
  }, []);

  // INVARIANT: the controller identity is STABLE across renders.
  // Why: it is an effect dep in useEditorCollab (chosen/teardown/bound drivers),
  // so a per-render object would re-run those effects every render and loop the
  // machine — refs and the dispatch are stable, memoizing on them yields one identity.
  return useMemo<EntitySwitchController>(() => ({
    get state() { return stateRef.current; },
    log: logRef.current,
    apply,
    isLiveFor: (entityId: string | undefined | null) => isLiveFor(stateRef.current, entityId),
  }), [apply]);
}

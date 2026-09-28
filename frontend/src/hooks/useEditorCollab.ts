/**
 * Collab lifecycle for the document editor.
 *
 * ARCH: absorbs the collab connection, overlay-grace, reconnect polling,
 * entity-switch reset, yCollab compartment binding, active-handle publish, and
 * entity-switch teardown from Editor.tsx — encapsulating the effect-ordering
 * invariants that make collab join/leave correct.
 *
 * Effect-registration order (PINNED — React registers effects in call order):
 *   1. switch driver (entity switch) — MUST precede the connection join so the
 *      machine enters `binding` BEFORE joinEntity runs; a synthesized re-attach
 *      onCollabInitDone then lands on a binding phase and is accepted.
 *   2–3. overlay-grace update + dispose.
 *   4. useCollabConnection (registers its own join/leave internally).
 *   5. yCollab compartment binding — phase-guarded (live for the current entity),
 *      so it never rebinds against a stale readiness from the previous entity.
 *   6. reconnect-info polling.
 *   7. handle publish — entity map (identity-scoped consumers) + guarded focused-slot
 *      claim, re-run on connEpoch (the connection landing after mount).
 *   8. entity-switch teardown — MUST follow useCollabConnection so its cleanup
 *      (which nulls the handle) runs AFTER the connection's own cleanup.
 *
 * viewEpoch ownership rule (PINNED): Editor.tsx OWNS viewEpoch (useState +
 * setViewEpoch); this hook consumes it as a READ-ONLY dep (yCollab rebind) and
 * NEVER calls setViewEpoch — avoids a re-render/ownership dep cycle.
 *
 * switchPhase ownership rule (PINNED): Editor.tsx OWNS the entity-switch phase
 * controller (useEntitySwitchPhase) and passes it in READ-ONLY; this hook DRIVES
 * it (chosen/cleared/teardown/bound/lost) but never re-creates it.
 */

// SYSTEM: editor-collab — collab WS lifecycle + yCollab binding for the document editor

import type React from 'react';
import { useState, useRef, useEffect, useCallback } from 'react';
import type { EditorView } from '@codemirror/view';
import { Compartment } from '@codemirror/state';
import { useTranslation } from '../i18n';
import { useAppStore } from '../store/app-store';
import { setEntityHandle, getEntityHandle } from '../collab/active-handle-registry';
import { publishHandle, releaseHandle, getActiveHandle, unmountView, type EditorRole } from '../editor/active-editor';
import type { EntityYjsState } from '../collab/yjs-provider';
import { useCollabConnection } from './useCollabConnection';
import { createOverlayGrace } from '../collab/overlay-grace';
import { flushPositionCache } from '../editor/position-cache';
import { createYjsExtension } from '../collab/yjs-binding';
import { collabPresence } from '../editor/collab-presence';
import { useProjectCollab } from '../collab/ProjectCollabContext';
import type { EntitySwitchController } from '../editor/entity-switch-phase';
import type { WsStatus } from '../collab/ws-status';
import type { Document, Reference } from '../types';

type ActiveItem = Document | Reference | null;

export interface UseEditorCollabParams {
  editorViewRef: React.RefObject<EditorView | null>;
  activeItemId: string | undefined;
  isReference: boolean;
  snapshotPreview: { content?: string } | null;
  /** Read-only epoch that bumps on every EditorView creation (owned by Editor.tsx). */
  viewEpoch: number;
  /** Ref to the deferred active item (used for teardown checkpoint capture). */
  activeItemRef: React.RefObject<ActiveItem>;
  /** Ref to the autosave checkpoint fn (Editor.tsx assigns it after useEditorAutosave). */
  checkpointRef: React.RefObject<(item: ActiveItem) => Promise<void>>;
  /** The entity-switch phase controller (owned by Editor.tsx; driven by this hook). */
  switchPhase: EntitySwitchController;
  /** Column role — gates the empty-slot claim (primary only; see the focusedRole INVARIANT in editor/active-editor). */
  role: EditorRole;
}
export interface UseEditorCollabResult {
  collabStatus: WsStatus;
  overlayHidden: boolean;
  reconnectInfo: { attempts: number; max: number };
  /** The connection's EntityYjsState ref (NOT the { handleRef, connEpoch } object —
   *  consumers read `.current`; connEpoch is consumed internally for re-publish). */
  collabConnectionRef: ReturnType<typeof useCollabConnection>['handleRef'];
  /** Compartment the yCollab binding reconfigures; Editor.tsx seeds it empty in cmExtensions. */
  yjsCompartment: React.RefObject<Compartment>;
  /** The machine is live for the entity being rendered — the editable/overlay gate. */
  liveForCurrent: boolean;
}

export function useEditorCollab({
  editorViewRef,
  activeItemId,
  isReference,
  snapshotPreview,
  viewEpoch,
  activeItemRef,
  checkpointRef,
  switchPhase,
  role,
}: UseEditorCollabParams): UseEditorCollabResult {
  const { t } = useTranslation();
  const projectCollab = useProjectCollab();

  const [collabStatus, setCollabStatus] = useState<WsStatus>('connecting');
  // INVARIANT: readiness is the phase machine's `live` state for the entity being
  // rendered — a dedicated collabReady boolean was its exact mirror and is gone.
  // Why: status==='connected' alone is insufficient — the WS is open but the
  // doc/version may not yet match the server; only an accepted `bound` (current
  // entity, binding phase) means init has applied.
  // INVARIANT: overlayHidden debounces the BLOCKING overlay only — collabStatus stays
  // truthful for the status dot + toast. Why: a mid-session reconnect under the grace
  // window recovers on the first retry; flashing a full-screen blocker interrupts typing.
  const [overlayHidden, setOverlayHidden] = useState(true);
  const overlayGraceRef = useRef<ReturnType<typeof createOverlayGrace> | null>(null);
  if (overlayGraceRef.current === null) {
    overlayGraceRef.current = createOverlayGrace(setOverlayHidden);
  }
  const [reconnectInfo, setReconnectInfo] = useState({ attempts: 0, max: 50 });
  const yjsExtCompartment = useRef(new Compartment());

  // ── Collab WS connection lifecycle ───────────────────────────────────
  // The machine's entity identity at callback time: init callbacks are stable
  // closures, so they must read the CURRENT entity through this ref to attribute
  // their bound event correctly.
  // INVARIANT(corruption): attribute `bound` from THIS ref, never from
  // switchPhase.state.entityId. Why: the grammar rejects a bound whose id differs
  // from the binding entity — sourcing the id from the machine makes that check
  // compare a value against itself, so it can never fail and the stale-init
  // rejection silently becomes a no-op.
  const entityIdRef = useRef(activeItemId);
  entityIdRef.current = activeItemId;

  const handleCollabInitDone = useCallback((_textChanged: boolean) => {
    // INVARIANT(corruption): only an init the phase grammar ACCEPTS raises
    // readiness. Why: the live phase gates editing; a rejected bound (wrong entity
    // or wrong phase) is by definition not an init for the entity the editor is
    // binding, and letting it through re-introduces the stale-init race.
    switchPhase.apply({ type: 'bound', id: entityIdRef.current ?? '' });
  }, [switchPhase]);

  // Tracks whether the connection-status toast currently on screen is ours.
  const statusToastRef = useRef(false);

  const handleCollabStatusChange = useCallback((status: WsStatus) => {
    setCollabStatus(status);
    // INVARIANT: any transition out of 'connected' drops the machine out of live.  Why: off-connected the session is unusable; dropping live immediately re-gates the editor instead of acting on stale ready state.
    if (status !== 'connected') {
      switchPhase.apply({ type: 'lost' });
    }
    const { showToast, clearToast } = useAppStore.getState();
    if (status === 'reconnecting') {
      showToast(t('collabReconnecting'), 'warning', { persistent: true });
      statusToastRef.current = true;
    } else if (status === 'offline') {
      showToast(t('collabStatus_offline'), 'error', { persistent: true });
      statusToastRef.current = true;
    } else if (status === 'connected') {
      // INVARIANT: clear the toast only when THIS handler raised one.
      // Why: the toast slot is global and single-entry, so an unconditional
      // clear on 'connected' wiped unrelated errors — measured live, it
      // swallowed the project-load failure toast in the frame it was raised
      // (useProjectConnection), bouncing the user to '/' with no message.
      if (statusToastRef.current) {
        clearToast();
        statusToastRef.current = false;
      }
    }
    }, [t, switchPhase]);

  // WHY: on entity switch, the machine enters binding for the NEXT entity.
  // INVARIANT(corruption): this effect MUST be registered before useCollabConnection's join effect.
  // Why: run order follows registration order — driving chosen here (before the join) makes the
  // machine enter binding BEFORE joinEntity runs, so on a re-attach the connection's synthesized
  // onCollabInitDone(false) lands on the binding phase and wins instead of being rejected as stale.
  const liveForCurrent = switchPhase.isLiveFor(activeItemId);
  useEffect(() => {
    if (activeItemId) {
      switchPhase.apply({ type: 'chosen', id: activeItemId });
    } else if (switchPhase.state.phase === 'leaving') {
      switchPhase.apply({ type: 'cleared' });
    }
    return () => {
      if (activeItemId) switchPhase.apply({ type: 'teardown', id: activeItemId });
    };
  }, [activeItemId, switchPhase]);

  // Drive the blocking-overlay grace window.
  useEffect(() => {
    const grace = overlayGraceRef.current;
    if (!grace) return;
    grace.update(collabStatus === 'connected' && liveForCurrent, collabStatus === 'offline');
  }, [collabStatus, liveForCurrent]);

  useEffect(() => () => overlayGraceRef.current?.dispose(), []);

  const { handleRef: collabConnectionRef, connEpoch } = useCollabConnection({
    entityId: activeItemId ?? null,
    isReference,
    onCollabInitDone: handleCollabInitDone,
    onStatusChange: handleCollabStatusChange,
  });

  // Wire yCollab into the editor view via compartment when both view and Yjs state are ready.
  // INVARIANT: phase-guarded — bind only when the machine is LIVE for the entity
  // being rendered.  Why: a rebind against stale readiness overwrites the fresh view with the previous entity's unsynced ytext — the flash-on-switch defect class.
  useEffect(() => {
    if (snapshotPreview) return; // live editor is unmounted during preview
    if (!liveForCurrent) return;
    const view = editorViewRef.current;
    const conn = collabConnectionRef.current;
    if (!view || !conn) return;
    const current = view.state.doc.toString();
    const synced = conn.ytext.toString();
    view.dispatch({
      changes: current !== synced
        ? { from: 0, to: current.length, insert: synced }
        : undefined,
      selection: { anchor: Math.min(view.state.selection.main.head, synced.length) },
      effects: [
        yjsExtCompartment.current.reconfigure([
          ...createYjsExtension(conn.ytext),
          ...collabPresence(
            conn.awareness.getAwareness(),
            conn.ytext,
            conn.ydoc,
            conn.reportSelection,
          ),
        ]),
        view.scrollSnapshot(),
      ],
    });
  }, [activeItemId, liveForCurrent, snapshotPreview, viewEpoch]);

  // Poll reconnect counter while reconnecting so overlay can show "attempt N of M".
  useEffect(() => {
    if (collabStatus !== 'reconnecting') return;
    const tick = () => {
      if (!projectCollab) return;
      setReconnectInfo({ attempts: projectCollab.reconnectAttemptsCount, max: projectCollab.maxReconnectAttempts });
    };
    tick();
    const id = setInterval(tick, 500);
    return () => clearInterval(id);
  }, [collabStatus, projectCollab]);

  // ARCH: Publish the collab handle on TWO axes (collab/active-handle-registry and the
  // focused handle in editor/active-editor):
  //  - the per-ENTITY map — identity-scoped consumers (table widget, badge list,
  //    panel ops, paste) resolve THIS entity's ydoc regardless of focus;
  //  - the FOCUSED slot — focus-inhering consumers (markdown actions, hotkeys,
  //    Cmd+S snapshot). The claim is GUARDED: only the focused column may write
  //    it, or the primary while the slot is empty (single-column mode IS
  //    role='primary' — INVARIANT in editor/active-editor.ts — so single-column
  //    first-paint is preserved; an unfocused secondary must never claim an
  //    empty slot: that would desync the slot from rootView/focusedRole and
  //    violate the slot's "*currently focused*" contract).
  // INVARIANT: re-publish on connEpoch. Why: the connection may land AFTER this
  // hook's first render (projectCollab arriving post-mount) — a mount-only
  // publish ran once against a null connection and the slot/map stayed empty
  // forever (blank table widget on first open in split view).
  const publishedRef = useRef<{ id: string | undefined; handle: EntityYjsState | null }>({ id: undefined, handle: null });
  const publishHandles = useCallback(() => {
    const conn = collabConnectionRef.current;
    const prev = publishedRef.current;
    if (activeItemId && conn) {
      setEntityHandle(activeItemId, conn);
    } else if (activeItemId && prev.id === activeItemId && prev.handle && getEntityHandle(activeItemId) === prev.handle) {
      // WHY: a null connection for the SAME entity releases OUR publication — the
      // preview-mode leave / conn drop bumps connEpoch with a null handle; skipping
      // this write left the map holding a LEFT ydoc (stale reads), and nulling
      // publishedRef below disarmed the teardown's previous-match guard so nothing
      // ever cleaned the entry.
      setEntityHandle(activeItemId, null);
    }
    // NB: `hasFocus` is a GETTER (boolean) on EditorView in @codemirror/view 6.x —
    // not a method; calling it throws.
    const mayClaimSlot = editorViewRef.current?.hasFocus
      || (getActiveHandle() === null && role === 'primary');
    if (conn && mayClaimSlot) {
      publishHandle(conn);
    } else if (!conn && prev.handle) {
      // Guarded release of our own slot claim (same leave path as the entity map).
      releaseHandle(prev.handle);
    }
    publishedRef.current = { id: activeItemId, handle: conn ?? null };
  }, [activeItemId, role, collabConnectionRef, editorViewRef]);
  useEffect(() => { publishHandles(); }, [publishHandles, connEpoch]);

  // Guarded teardown — nulls the slot/entity entries only while they still hold
  // OUR handle (previous-match guard, mirrors setRoleView in Editor.tsx). Why: a
  // secondary column's unmount must not wipe the primary's slot (and vice versa);
  // an unguarded null left the focused doc handle unpublished with nothing to
  // re-publish it (TableFocusView rendered blank until reload).
  useEffect(() => {
    return () => {
      const { id, handle } = publishedRef.current;
      if (handle) {
        releaseHandle(handle);
        if (id && getEntityHandle(id) === handle) setEntityHandle(id, null);
      }
      publishedRef.current = { id: undefined, handle: null };
    };
  }, [activeItemId]);

  // Flush position cache + snapshot content + leave collab session on document switch.
  useEffect(() => {
    if (!activeItemId) return;

    const leavingItem = activeItemRef.current;
    // INVARIANT: capture the collab handle here, in the effect body — not inside cleanup.
    // Why: useCollabConnection's effect is registered before this one, so its cleanup runs
    // first; reading collabConnectionRef.current from inside the cleanup would see a ref
    // that the next setup is about to overwrite. Closure capture freezes the leaving handle.
    const leavingHandle = collabConnectionRef.current;

    return () => {
      // INVARIANT(data-loss): on entity switch, checkpoint(prev) must resolve before leave(prev).  Why: leave tears down the collab channel; checkpoint must land first (.finally → leave) or the unsaved cursor/scroll position is lost on the switch.
      flushPositionCache();
      // A checkpoint failure toasts in content-sync persist — swallowed here so
      // the rejection never escapes the teardown, leave still runs via .finally.
      void checkpointRef.current?.(leavingItem)
        .catch(() => {})
        .finally(() => {
          leavingHandle?.leave();
        });
      const prev = editorViewRef.current;
      editorViewRef.current = null;
      unmountView(prev ?? undefined);
    };
  }, [activeItemId]);

  return {
    collabStatus,
    overlayHidden,
    reconnectInfo,
    collabConnectionRef,
    yjsCompartment: yjsExtCompartment,
    liveForCurrent,
  };
}

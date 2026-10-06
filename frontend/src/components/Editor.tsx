/**
 * Full-screen document editor with autosave and position persistence.
 *
 * Uses raw CodeMirror 6 EditorView via CodeMirrorEditor wrapper.
 * Document switching is handled by key={activeItemId} (unmount/remount).
 *
 * The component is the orchestrator (store reads, hook wiring, layout); the
 * CM6 extension wiring lives in ./editor submodules: editor-extension-bundle
 * (the live extension array + Mod-/ raw mode), region-agent-wiring
 * (pinned-region highlight + auto-unpin), preview-editor-extensions (the
 * snapshot-preview overrides). The position cache is ../editor/position-cache;
 * the transclusion map is ./editor/live-preview.
 *
 * Position cache: module-level Map persists cursor+scroll across remounts within the session.
 * Save strategy: the CRDT is the only write door — checkpoints flush pending ydoc
 * updates over the collab socket (content-sync persist).
 *
 * Store slices: currentDocument, currentReference, documents, isSaving, accessLevel.
 * Custom events dispatched: 'create-note-from-editor'.
 */
// WHY: key={activeItemId} forces full remount — no stale state between documents.
// SYSTEM: editor — CM6 editor orchestrating autosave (checkpoint), position cache, collab WS

import type React from 'react';
import { useState, useEffect, useRef, useCallback, useMemo, useDeferredValue } from 'react';
import { Compartment } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import CodeMirrorEditor from './editor/CodeMirrorEditor';
import { useTranslation } from '../i18n';
import { useAppStore } from '../store/app-store';
import { useUIStore } from '../store/ui-store';
import { SnapshotPreviewBanner } from './editor/SnapshotPreviewBanner';
import { PreviewDocumentBanner } from './editor/PreviewDocumentBanner';
import { CollabPresenceChips } from './editor/CollabPresenceChips';
import { ReferenceMediaBar } from './editor/ReferenceMediaBar';
import { mountTableTestHook } from './editor/dev-table-test-hook';
import { mountView, type EditorRole, setRoleView, setViewEntity } from '../editor/active-editor';
import { positionCacheGet } from '../editor/position-cache';
import { editableCompartment } from '../editor/editor-plugins';
import { useEditorAutosave } from '../hooks/useEditorAutosave';
import { useEditorPosition } from '../hooks/useEditorPosition';
import { useEditorReferenceSync } from '../hooks/useEditorReferenceSync';
import { useEditorEvents } from '../hooks/useEditorEvents';
import { useAnchorScroll } from '../hooks/useAnchorScroll';
import { useEditorHotkeys } from '../hooks/useEditorHotkeys';
import { useImageReferenceNav } from '../hooks/useImageReferenceNav';
import { EditorOfflineOverlay } from './collab/EditorOfflineOverlay';
import type { Document, Reference } from '../types';
import { useProjectCollab } from '../collab/ProjectCollabContext';
import { useEditorCollab } from '../hooks/useEditorCollab';
import { useEditorFileDrop } from './editor/useEditorFileDrop';
import { FileDropImportModal } from './editor/FileDropImportModal';
import { buildEditorExtensions } from './editor/editor-extension-bundle';
import { makeRegionResolver, useRegionChangedDispatch } from './editor/region-agent-wiring';
import { buildPreviewExtensions } from './editor/preview-editor-extensions';
import { EditorReferenceBar } from './editor/EditorReferenceBar';
import { useEntitySwitchPhase } from '../editor/entity-switch-phase';

// ─── Editor ───────────────────────────────────────────────────────────────────

interface EditorProps {
  // WHY: when `entity` is supplied (split view), the editor renders that entity
  // instead of computing the active item from the store. role='secondary' marks the
  // reference column; hideBanner suppresses the per-column banner (hoisted to the shared  Why: split view renders each column's own entity, not the shared store's active item
  // full-width banner). With no props, behaves exactly as the single global editor.
  // Why: split view mounts two Editor instances and the right column must be told what
  // to render, while the left column keeps the legacy store-driven path.
  entity?: Document | Reference;
  role?: EditorRole;
  hideBanner?: boolean;
}

export function Editor({ entity, role = 'primary', hideBanner = false }: EditorProps = {}) {
  const { t } = useTranslation();
  const compactLayout = useUIStore(s => s.compactLayout);
  const roleRef = useRef(role);
  roleRef.current = role;
  const currentDocument = useAppStore(s => s.currentDocument);
  const currentReference = useAppStore(s => s.currentReference);
  const previewDocument = useAppStore(s => s.previewDocument);
  const snapshotPreview = useAppStore(s => s.snapshotPreview);
  const accessLevel = useAppStore(s => s.accessLevel);
  const isReadonly = accessLevel !== 'full' || !!previewDocument;
  const previewScrollOffset = useAppStore(s => s.previewScrollOffset);
  // WHY: useDeferredValue keeps the urgent commit (banner colors, RefCard
  // highlight via store-direct reads) on the fast path; the heavy CM6 remount
  // and collab leave/join run in a deferred commit. Result: click → ack frame,
  // editor content swap follows.
  // INVARIANT: in split view the entity comes from props; the store-derived item is
  // only used by the single global editor. previewDocument/snapshot flows never co-occur
  // with split (ProjectPage renders the single Outlet path in those cases).  Why: split and preview/snapshot are mutually exclusive render paths
  const storeActiveItem = previewDocument || currentReference || currentDocument;
  const rawActiveItem = entity ?? storeActiveItem;
  const activeItem = useDeferredValue(rawActiveItem);
  const isReference = !!activeItem && 'reference_id' in activeItem;
  // Threading isReference into the useMemo([]) focus handler via a ref: that handler
  // is created once and reads live state through refs (roleRef, collabConnectionRef);
  // closing over isReference directly would freeze the value at first render.
  const isReferenceRef = useRef(isReference);
  isReferenceRef.current = isReference;
  // INVARIANT: activeItemId derives from the **deferred** activeItem so the
  // CM6 `key={activeItemId}` remount, collab join, and active-handle registry
  // all swap in the same deferred commit.  Why: deferred so remount + collab join + handle-registry swap atomically
  const activeItemId = activeItem
    ? ('reference_id' in activeItem ? activeItem.reference_id : activeItem.document_id)
    : undefined;
  // INVARIANT: layout chrome (banner + media bar + image branch) reads the
  // **deferred** reference so the bar's appearance/height change happens in
  // the same commit as the editor remount. Reading currentReference directly  Why: deferred so the bar's height change lands in the same commit as the remount
  // would split the swap across two frames (bar moves in frame 1, text in
  // frame 2) and cause a double layout jump on media_type changes.
  const activeReference = isReference ? (activeItem as import('../types').Reference) : null;

  // No-empty-flash (item 7a): the references LIST is metadata-only, so a freshly-opened
  // text reference has content === undefined while its body is lazy-fetched. While that
  // holds (and the server says the ref HAS content), show a loading state instead of an
  // empty editor — distinct from a genuinely-empty ref (has_content === false) per the
  // no-silent-degradation rule. Image refs render via ReferenceMediaBar, not the editor.
  const isRefContentLoading = !!activeReference
    && activeReference.media_type !== 'image'
    && activeReference.content === undefined
    && activeReference.has_content !== false;

  const [refIsEmpty, setRefIsEmpty] = useState(true);
  const refIsEmptySetterRef = useRef(setRefIsEmpty);
  refIsEmptySetterRef.current = setRefIsEmpty;

  const editorViewRef = useRef<EditorView | null>(null);
  // INVARIANT: bumped on every EditorView creation (handleCreateEditor). The yCollab
  // binding effect depends on it so it re-binds the LATEST view — not just on
  // activeItemId/live-phase/snapshotPreview changes. Why: the live editor can be
  // re-created without those deps changing (StrictMode mount→unmount→remount, or any
  // remount on snapshot-preview exit). Without this the effect reconciles a now-stale
  // view while the visible one keeps the pre-restore content (restore looks ignored).
  // viewEpoch is OWNED by Editor.tsx and passed into useEditorCollab as a READ-ONLY dep.
  const [viewEpoch, setViewEpoch] = useState(0);
  const activeItemRef = useRef(activeItem);
  // INVARIANT: plain-text ("notepad") mode is ephemeral and per-EditorView — all
  // visual extensions (rendering + syntax coloring + ligatures) live in a
  // compartment so they can be detached wholesale on the Mod-/ toggle, and they  Why: a compartment detaches all visual extensions in one call on the plain-text toggle
  // reset to rendered on any remount/document switch.
  // Why: user wants the bare source exactly as stored, with no persistence and
  // independent toggling per split-view column (hence a per-instance ref, not a
  // module-level compartment shared across columns).
  const renderCompartment = useRef(new Compartment());
  const rawModeRef = useRef(false);
  // INVARIANT: read the project collab provider once at the top level — never
  // inside callbacks/effects. Why: useProjectCollab is useContext; calling it in
  // an interval tick or click handler is a rules-of-hooks violation.
  const projectCollab = useProjectCollab();
  // Ref the autosave checkpoint fn — assigned after useEditorAutosave runs. The
  // collab teardown (inside useEditorCollab) reads it at cleanup time (entity switch).
  const checkpointRef = useRef<(item: typeof activeItem) => Promise<void>>(async () => {});

  activeItemRef.current = activeItem;

  // Refs for the live-headings extension: it reads the live values without being
  // a useMemo dep (cmExtensions is created once). activeItemIdRef mirrors the
  // deferred id so the extension writes headings under the entity the editor
  // actually renders; snapshotPreviewRef disables it during snapshot preview so
  // preview content never hijacks the TOC.
  const activeItemIdRef = useRef(activeItemId);
  activeItemIdRef.current = activeItemId;
  const snapshotPreviewRef = useRef(snapshotPreview);
  snapshotPreviewRef.current = snapshotPreview;

  // ── File-drop import (extracted — see useEditorFileDrop) ────────────
  // INVARIANT: file-drop import requires the same editable gate as typing —
  // readonly viewers / disconnected collab must not insert. Read via ref from the
  // drop handler (cmExtensions is created once, can't close over live state).  Why: readonly viewers and disconnected collab must not be able to insert content
  // The ref is assigned next to the `editable` computation below.
  const dropGuardRef = useRef({ isReadonly, editable: false });
  const getDropGuard = useCallback(() => dropGuardRef.current, []);
  const { fileDropExtension, modalProps: fileDropModalProps } = useEditorFileDrop({
    editorViewRef, activeItemRef, getDropGuard,
  });

  // ── Extracted hooks ─────────────────────────────────────────────────
  const { restoreScrollPosition } = useEditorPosition({
    editorViewRef, activeItemRef,
  });

  // ── Collab WS connection lifecycle (extracted) ──────────────────────────
  // The entity-switch phase machine (idle → leaving → binding → live) is owned
  // HERE and driven by useEditorCollab's effects. Derived state below (editable,
  // yCollab binding) reads the phase instead of a readiness boolean: the machine
  // never shows a live phase for a non-current entity, so the stale-render race
  // is gone by construction.
  const switchPhase = useEntitySwitchPhase();
  const {
    collabStatus,
    overlayHidden,
    reconnectInfo,
    collabConnectionRef,
    yjsCompartment,
    liveForCurrent,
  } = useEditorCollab({
    editorViewRef,
    activeItemId,
    isReference,
    snapshotPreview,
    viewEpoch,
    activeItemRef,
    checkpointRef,
    switchPhase,
    role,
  });

  // DEV-ONLY repro hook for the table cell click/focus race: lets an e2e spec poll
  // whether the collab handle is ready and insert a table without the timing-flaky
  // Shift-Mod-T hotkey path. See dev-table-test-hook.ts.
  // DEBT: dev test hook — Why deferred: nested-cell focus race can't be observed without
  // a deterministic table-seed; the hotkey path needs a synced handle the e2e stand lacks.
  useEffect(() => {
    mountTableTestHook();
  }, []);

  const { checkpoint } = useEditorAutosave({
    editorViewRef,
    getCollabStatus: () => collabConnectionRef.current?.synced ? 'connected' as const : 'connecting' as const,
  });
  // Hand the checkpoint fn to the collab teardown (useEditorCollab) via ref, so the
  // entity-switch cleanup can checkpoint-then-leave in the correct order.
  checkpointRef.current = checkpoint;

  useEditorReferenceSync({ editorViewRef });
  // WHY: global editor events (TOC scroll, navigate-to-reference, note-link
  // removal) target the document column only. Disabled on the secondary (reference)
  // instance so they don't fire twice / mutate the reference content.  Why: the secondary reference instance must not double-fire events or mutate reference content
  useEditorEvents({ editorViewRef, currentDocument, enabled: role !== 'secondary' });
  // URL-hash anchor (#slug) → scroll to heading; primary editor only, same reason.
  useAnchorScroll({ editorViewRef, currentDocument, enabled: role !== 'secondary' });

  useEffect(() => {
    if (previewDocument && previewScrollOffset != null && editorViewRef.current) {
      setTimeout(() => {
        editorViewRef.current?.dispatch({
          effects: EditorView.scrollIntoView(previewScrollOffset, { y: 'center' }),
        });
      }, 100);
    }
  }, [previewDocument?.document_id, previewScrollOffset]);

  // see SYSTEM: selection-region-agent — resolve the active pinned region against the
  // focused ydoc for the live highlight. Stable closure reading live state at call
  // time; the branch table lives in region-agent-wiring (with its tests).
  const regionResolver = useMemo(
    () => makeRegionResolver(() => activeItemIdRef.current),
    [],
  );
  // Stable extensions array — created once, never re-created on re-renders.
  // WHY: deps intentionally limited — reconnect only when document/reference changes.
  // Refs capture live values without needing to be deps.
  // The array itself (and every comment that explains an entry) lives in
  // editor-extension-bundle.
  const cmExtensions = useMemo(() => buildEditorExtensions({
    renderCompartment: renderCompartment.current,
    rawModeRef,
    fileDropExtension,
    yjsCompartment: yjsCompartment.current,
    activeItemRef,
    refIsEmptySetterRef,
    activeItemIdRef,
    snapshotPreviewRef,
    roleRef,
    isReferenceRef,
    collabConnectionRef,
    regionResolver,
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), []);

  // WHY: the snapshot-preview instance is a pure VIEWER sharing the live extension
  // array (identical rendering by construction); the read-only overrides + the
  // checkpoint's captured table doc live in preview-editor-extensions.
  const previewCmExtensions = useMemo(
    () => buildPreviewExtensions(cmExtensions, snapshotPreview?.tables_json ?? null),
    [cmExtensions, snapshotPreview],
  );

  // INVARIANT: editor is online-only — input accepted iff collab is connected
  // AND the machine is live for the entity being rendered AND user has write access.
  // Why: ws.send() is a no-op on a closed socket and CRDT state is re-synced on
  // reconnect init. Accepting input offline produces silent data loss. The phase
  // check (live + current entity) replaces the old collabReady boolean and the
  // call-order invariant it needed: live is entered only after the yCollab
  // binding effect has bound the current entity, and never for a stale one.
  const editable = !isReadonly && collabStatus === 'connected' && liveForCurrent;
  // Live values for the file-drop gate (INVARIANT at the dropGuardRef declaration).
  dropGuardRef.current = { isReadonly, editable };
  useEffect(() => {
    const view = editorViewRef.current;
    if (!view) return;
    view.dispatch({ effects: editableCompartment.reconfigure(EditorView.editable.of(editable)) });
    // One-shot: focus the editor when it becomes editable after a programmatic
    // entity open that requested focus (e.g. creating a reference). The remount's
    // view.focus() doesn't stick because contenteditable is false during collab join.
    // WHY: matched by entity id, not role — a new reference renders in the primary
    // editor (center), the split column or the Refs tab (quick preview), and only
    // the editor showing it may take focus.
    if (editable && activeItemId && useAppStore.getState().pendingEditorFocus === activeItemId) {
      useAppStore.getState().setPendingEditorFocus(null);
      view.focus();
    }
    // INVARIANT: this effect must re-run on EVERY EditorView creation, not only on
    // an `editable` value change. Why: a fresh view is seeded non-editable (see the
    // INVARIANT(data-loss) in editor-extension-bundle), and a view created while
    // `editable` is already true (doc-switch loading branch where collab sync beat
    // the REST hydrate; snapshot-preview exit remount) never receives the
    // reconfigure — the editor stays read-only until reload.
  }, [editable, activeItemId, viewEpoch]);

  // see SYSTEM: selection-region-agent — re-trigger highlight resolution when the
  // active region changes WITHOUT a CM6 transaction (pin/unpin/switch, ghost
  // pin/clear): subscribes and dispatches the regionChanged-annotated no-op.
  // Selectors + the WHY on the annotation live in region-agent-wiring.
  useRegionChangedDispatch(editorViewRef);

  // WHY: the CM6 container mounts as .editor-cm--pending (visibility:hidden) until
  // onReady. The scroll target is set at creation (below) so the parser settles over
  // the RESTORED viewport, but the decoration pass still moves line heights; revealing
  // only after the final restore is what keeps the user from seeing content land and
  // then lurch. Bound to a view, not the ref: a stale timer must not reveal a successor.
  const revealEditor = useCallback((view: EditorView | null) => {
    view?.dom.parentElement?.classList.remove('editor-cm--pending');
  }, []);

  const handleCreateEditor = useCallback((view: EditorView) => {
    editorViewRef.current = view;
    // Scroll before the first paint: CM6 applies the target in its initial measure,
    // so the viewport (and therefore the syntax-tree readiness check that gates
    // onReady) is already the saved region, not the top of the document.
    restoreScrollPosition();
    // Safety net: an editor that never reports ready must not stay invisible.
    setTimeout(() => revealEditor(view), 1500);
    setViewEpoch(e => e + 1);
    mountView(view);
    // WHY: register this column's view under its
    // role so the note connector + click handler can resolve a note's OWNING column
    // by role (not by focus). Split view mounts primary + secondary simultaneously.
    setRoleView(roleRef.current, view);
    // WHY: register the entity this view renders so identity-scoped non-React
    // consumers (table widget late-bind, paste/drop model placement) can resolve
    // the view's OWN ydoc — the focused slot cannot answer that in split view.
    setViewEntity(view, activeItemIdRef.current ?? null);
    const item = activeItemRef.current;
    if (item && 'reference_id' in item) {
      refIsEmptySetterRef.current(view.state.doc.length === 0);
    }
    // Image references are viewed, not edited — never steal focus into the text
    // editor on open. Keeps arrow-key image navigation working (the editable-zone
    // gate would otherwise block cycling once focus lands in .cm-content).
    if (item && 'reference_id' in item && item.media_type === 'image') return;
    // INVARIANT: guard EVERY rename surface, not just doc-rename-input. All
    // rename inputs carry data-rename-input; closest() also covers nested/future
    // ones. Why: a class-equality check here left the ref/table/breadcrumb inputs
    // unprotected, so editor focus() fired their onBlur mid-rename.
    if (!document.activeElement?.closest('[data-rename-input]')) {
      view.focus();
    }
  }, [restoreScrollPosition, revealEditor]);

  const handleEditorReady = useCallback(() => {
    restoreScrollPosition();
    revealEditor(editorViewRef.current);
    const item = activeItemRef.current;
    if (item && 'reference_id' in item && item.media_type === 'image') return;
    // Same rename-input focus guard as handleCreateEditor — see INVARIANT there.
    if (!document.activeElement?.closest('[data-rename-input]')) {
      editorViewRef.current?.focus();
    }
  }, [restoreScrollPosition, revealEditor]);

  // [snapshotPreview] effect — checkpoint content on snapshot enter.
  // Per-document dedup in checkpointContent prevents double WS flush under React strict mode.
  // A persist failure toasts in content-sync; nothing to act on here.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!snapshotPreview) return;
    checkpoint(activeItemRef.current).catch(() => {});
  }, [snapshotPreview]);

  // WHY: clear this column's role-slot on unmount.
  // Stable (runs once per mount); key={activeItemId} remount re-registers via
  // handleCreateEditor. The `previous`-match guard in setRoleView ensures a secondary
  // unmount (split exit) never nulls the primary slot — mirrors the focused/root
  // registry teardown invariants in useEditorCollab.
  useEffect(() => () => {
    setRoleView(roleRef.current, null, editorViewRef.current ?? undefined);
  }, []);

  // Cmd+S → snapshot modal (document column only — see hook INVARIANT).
  useEditorHotkeys({ role, isReference });
  useImageReferenceNav(role);

  const savedCursor = useMemo(() => {
    if (!activeItemId) return undefined;
    const pos = positionCacheGet(activeItemId)
      || useUIStore.getState().documentPositions[activeItemId];
    return pos?.cursor;
  }, [activeItemId]);


  if (!activeItem) {
    return (
      <div className="flex-1 w-full h-full flex items-center justify-center text-text-dim bg-bg">
        {t('selectDocOrRef')}
      </div>
    );
  }

  const cmEditMode = !snapshotPreview;

  return (
    <div className="flex-1 overflow-hidden flex flex-col bg-bg relative" data-switch-phase={switchPhase.state.phase}>
      <CollabPresenceChips />
      <SnapshotPreviewBanner />
      <PreviewDocumentBanner />

      {activeReference && currentDocument && !snapshotPreview && !hideBanner && (
        <EditorReferenceBar
          reference={activeReference}
          isReadonly={isReadonly}
          isEmpty={refIsEmpty}
        />
      )}

      <div
        className="editor-scroll"
        onClick={!snapshotPreview ? handleScrollAreaClick : undefined}
        style={cmEditMode && activeReference?.media_type !== 'image' ? { overflow: 'hidden' } : undefined}
      >
        {activeReference?.media_type === 'image' && !snapshotPreview && (
          <ReferenceMediaBar
            reference={activeReference}
            canEdit={!isReadonly}
          />
        )}
        <div className={`editor-content${compactLayout ? ' editor-content--compact' : ''}`} style={cmEditMode && activeReference?.media_type !== 'image' ? { height: '100%', minHeight: 0, width: '100%', padding: 0 } : cmEditMode ? { minHeight: 0, width: '100%', padding: 0 } : undefined}>
          {snapshotPreview ? (
            <CodeMirrorEditor
              key={snapshotPreview.checkpoint_id}
              doc={snapshotPreview.content ?? ''}
              extensions={previewCmExtensions}
              className="snapshot-preview"
            />
          ) : isRefContentLoading ? (
            <div className="flex items-center justify-center h-full text-text-dim text-ui-base">
              {t('loading')}
            </div>
          ) : (
            <div className="relative h-full w-full">
              <CodeMirrorEditor
                key={activeItemId}
                doc={activeItem.content || ''}
                extensions={cmExtensions}
                initialCursor={savedCursor}
                onReady={handleEditorReady}
                onCreateView={handleCreateEditor}
                className="editor-cm editor-cm--pending"
              />
              <span
                className={`collab-status-dot collab-status-dot--${collabStatus}`}
                title={t(`collabStatus_${collabStatus}`)}
              />
              {/* INVARIANT: overlay covers the CM6 area whenever editing is disabled by
                 connection state. Why: a top-bar toast is dismissible and easy to miss;
                 users typing into a "stuck" editor under the assumption their input
                 will queue is the failure mode this overlay prevents. Sidebar/header
                 stay interactive — only the editor surface is locked. */}
              {!isReadonly && !overlayHidden && (
                <EditorOfflineOverlay
                  status={collabStatus}
                  collabReady={liveForCurrent}
                  reconnectAttempts={reconnectInfo.attempts}
                  maxAttempts={reconnectInfo.max}
                  onRetry={() => projectCollab?.connect()}
                />
              )}
            </div>
          )}
        </div>
      </div>

      <FileDropImportModal {...fileDropModalProps} />
    </div>
  );

  // Click outside text lines → place cursor at start/end
  function handleScrollAreaClick(e: React.MouseEvent<HTMLDivElement>) {
    if (!editorViewRef.current) return;
    const item = activeItemRef.current;
    // Image references: don't move focus into the text editor on padding clicks.
    if (item && 'reference_id' in item && item.media_type === 'image') return;
    const view = editorViewRef.current;
    const scroller = view.dom.querySelector('.cm-scroller');
    if (scroller?.contains(e.target as Node)) return;
    const scrollerRect = (scroller ?? view.dom).getBoundingClientRect();
    if (e.clientY < scrollerRect.top) {
      view.dispatch({ selection: { anchor: 0 } });
    } else {
      view.dispatch({ selection: { anchor: view.state.doc.length } });
    }
    view.focus();
  }
}

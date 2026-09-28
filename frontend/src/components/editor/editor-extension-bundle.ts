/**
 * The live editor's CM6 extension bundle — the array the Editor component
 * mounts. Created once per Editor mount, never re-created on re-renders;
 * everything time-varying is read through refs/callbacks so the array stays
 * referentially stable.
 *
 * // see SYSTEM: editor — the CM6 wiring half of the Editor component.
 *
 * Everything visual (structural rendering + syntax coloring + ligatures) is
 * created here ONCE (renderExtensions) and toggled as a group for plain-text
 * mode (Mod-/) through the caller-owned per-instance render compartment. The
 * document's render fields stay in that compartment (entity-specific); the
 * shared editor-experience core (setup, theme, markdown, wrapping,
 * transclusion depth/reveal) is the SAME source of truth as table cells and
 * nested views.
 */
import { type RefObject } from 'react';
import { Compartment, Prec, type Extension } from '@codemirror/state';
import { EditorView, keymap } from '@codemirror/view';
import type { Document, Reference } from '../../types';
import type { EditorRole } from '../../editor/active-editor';
import { pasteHandlerExtension } from '../../editor/paste-handler';
import { renderExtensions, editorExperience } from './render-bundle';
import { scrollAnchorPlugin } from './live-preview';
import { tableReconcileExtension } from './live-preview/table-reconcile';
import { voiceWidgetExtension } from './voice-widget';
import { createObserverExtension } from '../../editor/editor-observer';
import { paddingClickFocus } from '../../editor/padding-click-focus';
import { linkClickExtension, editableCompartment } from '../../editor/editor-plugins';
import { buildKeymapExtension } from './hotkey-keymap';
import { listContinuationExtension } from './list-continuation';
import { listNavExtension } from './list-nav';
import { liveHeadingsExtension } from '../../editor/live-headings-extension';
import { regionHighlightExtension, type RegionRange } from '../../editor/region-highlight';
import { claimFocus, type FocusClaim } from '../../editor/active-editor';
import { regionLostListener } from './region-agent-wiring';

export interface EditorExtensionDeps {
  /** Per-instance visual compartment (NOT module-level: split view toggles columns independently). */
  renderCompartment: Compartment;
  /** Live raw-mode flag — flipped by the Mod-/ toggle, read by nothing else here. */
  rawModeRef: { current: boolean };
  /** File-drop import extension from useEditorFileDrop. */
  fileDropExtension: Extension;
  /** yCollab binding compartment from useEditorCollab. */
  yjsCompartment: Compartment;
  /** Live active item (document or reference) the editor renders. */
  activeItemRef: RefObject<(Document | Reference | null)>;
  /** Setter for the reference-isEmpty flag (observer-driven). */
  refIsEmptySetterRef: { current: (empty: boolean) => void };
  /** Live deferred entity id — the live-headings extension writes under it. */
  activeItemIdRef: { current: string | undefined };
  /** Live snapshot-preview — disables live-headings + table-reconcile while previewing. */
  snapshotPreviewRef: { current: unknown };
  /** Live column role for focus routing. */
  roleRef: { current: EditorRole };
  /** Live is-reference flag for focus routing. */
  isReferenceRef: { current: boolean };
  /** Live collab connection handle for focus routing. */
  collabConnectionRef: { current: FocusClaim['handle'] };
  /** Region resolver (region-agent-wiring) — drives the pinned-region highlight. */
  regionResolver: () => RegionRange | null;
}

/**
 * Mod-/ plain-text toggle — detaches/reattaches the visual render extensions
 * through the caller's per-instance compartment. The returned handler is the
 * keymap `run` fn; mount it at Prec.highest so it beats both CM6's default
 * toggleComment and buildKeymapExtension's Prec.high. Lives here (not in the
 * global markdownActionRegistry) because it needs the view's per-instance
 * renderCompartment.
 */
export function makeRawModeToggle(
  renderCompartment: Compartment,
  rawModeRef: { current: boolean },
  renderExt: Extension,
): (view: EditorView) => boolean {
  return (view) => {
    rawModeRef.current = !rawModeRef.current;
    view.dispatch({
      effects: renderCompartment.reconfigure(
        rawModeRef.current ? [] : renderExt,
      ),
    });
    return true;
  };
}

/** Assemble the live editor's extension array. See module docstring. */
export function buildEditorExtensions(deps: EditorExtensionDeps): Extension[] {
  // Everything visual (structural rendering + syntax coloring + ligatures) lives in
  // the shared render-bundle so the main editor and the nested transclusion view stay
  // identical by construction. Toggled off as a group for plain-text mode (Mod-/).
  const renderExt = renderExtensions();
  return [
    // Shared editor-experience core (setup, theme, markdown, wrapping, transclusion
    // depth/reveal) — the SAME source of truth as table cells and nested views. The
    // document's render fields stay in the Mod-/ compartment below (entity-specific).
    editorExperience({ mode: 'document' }),
    listContinuationExtension,
    listNavExtension,
    // INVARIANT(data-loss): a fresh view starts NON-editable; only the editable
    // effect in Editor.tsx opens it, once the phase machine is live for the entity
    // being rendered. Why: key={activeItemId} builds a new view on every entity
    // switch, so seeding `true` made each switch editable for ~200ms while collab
    // was still binding — and keystrokes landing there are wiped wholesale by the
    // yCollab bind (`insert: synced` over the whole doc), i.e. silently lost.
    // Measured live: 4 consecutive samples in phase `binding` with contenteditable=true.
    editableCompartment.of(EditorView.editable.of(false)),
    // All visual formatting (see renderExtensions above). Toggled off as a group
    // for plain-text mode via Mod-/ — see renderCompartment note in the Editor.
    deps.renderCompartment.of(renderExt),
    scrollAnchorPlugin,
    linkClickExtension,
    pasteHandlerExtension,
    // see SYSTEM: editor — clipboard copy handler lives in the shared
    // editorExperience() (render-bundle), so the document gets it here too.
    // File-drop import (drag a .docx/text file into the body) — see useEditorFileDrop.
    deps.fileDropExtension,
    // see SYSTEM: table-block — clone a pasted duplicate anchor to its own model and drop a
    // model whose anchor line was deleted. Debounced host-doc trigger; see table-reconcile.
    // WHY: disabled on the snapshot-preview instance (a pure viewer) — same
    // belt-and-braces pattern as liveHeadingsExtension, on top of the fire-time doc
    // identity guard in table-reconcile.  Why: the preview is a pure viewer — belt-and-braces over the fire-time identity guard
    tableReconcileExtension(() => !deps.snapshotPreviewRef.current),
    liveHeadingsExtension(
      () => deps.activeItemIdRef.current,
      () => !deps.snapshotPreviewRef.current,
    ),
    deps.yjsCompartment.of([] as Extension),
    createObserverExtension({
      activeItemRef: deps.activeItemRef,
      refIsEmptySetterRef: deps.refIsEmptySetterRef,
    }),
    paddingClickFocus,
    voiceWidgetExtension,
    buildKeymapExtension(),
    // see SYSTEM: selection-region-agent — live pinned-region highlight (re-resolves the
    // RelativePosition on every transaction via the facet resolver) + reactive
    // auto-unpin when the anchor is lost. See region-agent-wiring.
    regionHighlightExtension(deps.regionResolver),
    regionLostListener(() => deps.activeItemIdRef.current),
    // Mod-/ toggles plain-text mode by detaching/reattaching all visual
    // extensions through this view's per-instance renderCompartment.
    Prec.highest(keymap.of([{
      key: 'Mod-/',
      preventDefault: true,
      run: makeRawModeToggle(deps.renderCompartment, deps.rawModeRef, renderExt),
    }])),
    // WHY: focus-driven routing for split view. On focus this column claims the
    // focus-sensitive slots (view registry, active handle, focused role) via
    // claimFocus — the SOLE mutator of those slots from the Editor component — so
    // hotkeys/snapshot/floating-UI target whichever column the user is editing.
    EditorView.domEventHandlers({
      focus: (_e, view) => {
        claimFocus({
          role: deps.roleRef.current,
          isReference: deps.isReferenceRef.current,
          view,
          handle: deps.collabConnectionRef.current,
        });
        return false;
      },
    }),
  ];
}

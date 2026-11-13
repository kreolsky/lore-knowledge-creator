/** Floating toolbar that appears on text selection for quick formatting (suppressed while the docked bar is on; note-only bars stay). Store slices: selectionEmpty. Events consumed: show-link-suggestions, editor-selection-change. */

import type React from 'react';
import { useState, useEffect, useCallback, useLayoutEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import { EditorView } from '@codemirror/view';
import { StickyNote, MessageSquareQuote } from 'lucide-react';
import { markdownActionRegistry, canQuoteToChat, isHostTextView } from './markdown-actions';
import { isPanelPreviewView } from '../../editor/editor-plugins';
import { FormattingButtons, detectActiveStyles, NO_STYLES, ICON_SIZE, type ActiveStyles } from './FormattingButtons';
import { useDockedToolbarOn } from './DockedFormatToolbar';
import { selectedAtomicRange } from './live-preview/atomic-delete-guard';
import { useUIStore } from '../../store/ui-store';
import { useAppStore } from '../../store/app-store';
import { usePublicCanNote } from '../../store/public-notes-store';
import { on, off } from '../../events';
import { useEditorView } from '../../editor/active-editor';
import { useTranslation } from '../../i18n';
import { Button } from '../ui';
import { isArtifactSourceView } from './artifact-source-extensions';

interface ToolbarAnchor {
  centerX: number;
  belowTop: number;
  aboveBottom: number;
}

/** Compute anchor rects from the current selection — actual placement decided post-measure. */
function computeAnchor(view: EditorView): ToolbarAnchor | null {
  const { from, to } = view.state.selection.main;
  const fromCoords = view.coordsAtPos(from);
  const lastSelPos = Math.max(from, to - 1);
  const toCoords = view.coordsAtPos(lastSelPos);
  if (!fromCoords || !toCoords) return null;

  const multiLine = fromCoords.top !== toCoords.top;
  let centerX: number;
  if (multiLine) {
    const editorRect = view.dom.getBoundingClientRect();
    centerX = editorRect.left + editorRect.width / 2;
  } else {
    centerX = (fromCoords.left + toCoords.right) / 2;
  }

  return { centerX, belowTop: toCoords.bottom + 6, aboveBottom: fromCoords.top - 6 };
}

// Gap between the pointer tip and the guest bar's top edge (the chat Clarify button's).
const CURSOR_GAP = 14;

export default function SelectionToolbar() {
  const { t } = useTranslation();
  const getEditorView = useEditorView();
  const selectionEmpty = useUIStore(s => s.selectionEmpty);
  const accessLevel = useAppStore(s => s.accessLevel);
  // ARCH: full-access users get the formatting toolbar.
  // A commentator may note but not edit: formatting stays hidden and the bar shows
  // ONE wide labeled «Add note» button — so does a guest on a public link with comments on.
  // Readonly sees nothing.
  // Why the label: a lone icon is too small to notice when it is the only action.
  const isPublicShare = useUIStore(s => s.isPublicShare);
  const publicCanNote = usePublicCanNote();
  const canEdit = accessLevel === 'full' && !isPublicShare;
  const canNoteOnly = accessLevel === 'commentator' || (isPublicShare && publicCanNote);
  // The public guest's bar: one strong-yellow "Add note" button under the pointer.
  const guestBar = isPublicShare && !canEdit;
  const [visible, setVisible] = useState(false);
  const [offscreen, setOffscreen] = useState(false);
  const [anchor, setAnchor] = useState<ToolbarAnchor>({ centerX: 0, belowTop: 0, aboveBottom: 0 });
  const [styles, setStyles] = useState<ActiveStyles>(NO_STYLES);
  // Style menu or colour picker open — a change re-measures the bar's placement.
  const [popupOpen, setPopupOpen] = useState(false);
  const dockedOn = useDockedToolbarOn();
  const toolbarRef = useRef<HTMLDivElement>(null);
  const mouseDownRef = useRef(false);
  // Where the pointer released the selection; null after a keyboard selection. State,
  // not a ref: the release can land after the bar is already shown (the listeners
  // attach on the first selection, mid-drag), and it must move the bar then.
  const [pointer, setPointer] = useState<{ x: number; y: number } | null>(null);
  const hasPendingRef = useRef(false);

  const hide = useCallback(() => {
    setVisible(false);
    hasPendingRef.current = false;
  }, []);

  const showToolbar = useCallback(() => {
    const view = getEditorView();
    if (!view) return;
    const { from, to } = view.state.selection.main;
    if (from === to) { hide(); return; }
    // An exactly-one-image selection belongs to the ImageToolbar (the atomic
    // block's own bar): formatting actions are meaningless on a replaced widget.
    // WHY: a whole-selected object has nothing to format; an image has its own bar.
    if (selectedAtomicRange(view.state)) { hide(); return; }
    const a = computeAnchor(view);
    if (!a) return;
    setAnchor(a);
    setStyles(detectActiveStyles(view));
    setOffscreen(false);
    setVisible(true);
  }, [hide, getEditorView]);

  // WHY: getEditorView() is null at mount time (CM6 view is created async via
  // handleCreateEditor). We attach mouse listeners lazily on first selection event,
  // scoped to scrollDOM so sidebar/panel clicks don't interfere with pending state.
  const attachedDomRef = useRef<HTMLElement | null>(null);
  const attachMouseRef = useRef<() => void>(() => {});

  useEffect(() => {
    const onEditorMouseDown = () => { mouseDownRef.current = true; setPointer(null); };
    const onEditorMouseUp = (e: MouseEvent) => {
      mouseDownRef.current = false;
      setPointer({ x: e.clientX, y: e.clientY });
      if (hasPendingRef.current) {
        hasPendingRef.current = false;
        showToolbar();
      }
    };

    attachMouseRef.current = () => {
      const dom = getEditorView()?.scrollDOM;
      if (!dom || dom === attachedDomRef.current) return;
      attachedDomRef.current?.removeEventListener('mousedown', onEditorMouseDown);
      dom.addEventListener('mousedown', onEditorMouseDown);
      attachedDomRef.current = dom;
    };

    // WHY on document, not scrollDOM: a drag released outside the editor never
    // delivers mouseup to scrollDOM, so the drag never ended and the selection made
    // got no toolbar until the user collapsed it and selected again.
    const onDocMouseUp = (e: MouseEvent) => { if (mouseDownRef.current) onEditorMouseUp(e); };
    document.addEventListener('mouseup', onDocMouseUp);

    const onLinkPopup = () => hide();
    on('show-link-suggestions', onLinkPopup);

    return () => {
      off('show-link-suggestions', onLinkPopup);
      document.removeEventListener('mouseup', onDocMouseUp);
      attachedDomRef.current?.removeEventListener('mousedown', onEditorMouseDown);
      attachedDomRef.current = null;
    };
  }, [hide, showToolbar, getEditorView]);

  // React to selectionEmpty changes from Zustand (set by Editor.tsx updateListener)
  useEffect(() => {
    attachMouseRef.current();
    if (selectionEmpty) { hide(); return; }
    if (mouseDownRef.current) { hasPendingRef.current = true; return; }
    showToolbar();
  }, [selectionEmpty, hide, showToolbar]);

  // Reposition toolbar on selection range changes while already visible
  useEffect(() => {
    if (!visible) return;
    // WHY skip empty: the feed also fires on caret moves; hiding is owned by selectionEmpty.
    const onSelChange = ({ empty }: { empty: boolean }) => {
      if (empty) return;
      const view = getEditorView();
      if (!view) return;
      const { from, to } = view.state.selection.main;
      if (from === to) { hide(); return; }
      if (mouseDownRef.current) { hasPendingRef.current = true; return; }
      setStyles(detectActiveStyles(view));
      requestAnimationFrame(() => {
        const v = getEditorView();
        if (!v) return;
        const a = computeAnchor(v);
        if (a) setAnchor(a);
      });
    };
    on('editor-selection-change', onSelChange);
    return () => off('editor-selection-change', onSelChange);
  }, [visible, hide, getEditorView]);

  // WHY: the bar is position:fixed, so its anchor is a viewport point — re-derive it
  // on any ancestor scroll (capture phase) or it stays put while the text moves away.
  // A selection scrolled out of the editor's visible box hides the bar until it returns.
  // The guest bar is exempt: it deliberately stays under the pointer.
  useEffect(() => {
    if (!visible || guestBar) return;
    let frame = 0;
    const onScroll = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const v = getEditorView();
        if (!v) return;
        const a = computeAnchor(v);
        const box = v.scrollDOM.getBoundingClientRect();
        const inView = !!a && a.belowTop - 6 > box.top && a.aboveBottom + 6 < box.bottom;
        setOffscreen(!inView);
        if (a) setAnchor(a);
      });
    };
    document.addEventListener('scroll', onScroll, true);
    return () => { cancelAnimationFrame(frame); document.removeEventListener('scroll', onScroll, true); };
  }, [visible, guestBar, getEditorView]);

  // Escape to dismiss
  useEffect(() => {
    if (!visible) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') hide(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [visible, hide]);

  // Click outside toolbar (and outside the editor) to dismiss
  useEffect(() => {
    if (!visible) return;
    const onDown = (e: MouseEvent) => {
      // WHY the editor is exempt: a double/triple click selects the word/line inside
      // CM6's own mousedown handler, so the bar opens mid-event and this listener,
      // reached later by the same mousedown, closed it at once. Inside the editor the
      // selection feed already decides: a click that collapses it hides the bar.
      if (getEditorView()?.scrollDOM.contains(e.target as Node)) return;
      if (toolbarRef.current && !toolbarRef.current.contains(e.target as Node)) hide();
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [visible, hide, getEditorView]);

  // ARCH: final position is decided post-measure in a layout effect so the toolbar
  // can flip above the selection when there's no room below, and clamp horizontally
  // by its actual width (transform: translateX(-50%) centers on `left`).
  useLayoutEffect(() => {
    const el = toolbarRef.current;
    if (!visible || !el) return;
    const w = el.offsetWidth;
    const h = el.offsetHeight;

    // WHY: the guest's bar sits under the pointer, like the chat's Clarify button — a
    // selection dragged bottom-up leaves the pointer at its top edge, and a bar under
    // the selection rect would sit a whole selection away from it.
    if (guestBar && pointer) {
      const half = w / 2;
      el.style.left = `${Math.max(half + 8, Math.min(pointer.x, window.innerWidth - 8 - half))}px`;
      el.style.top = `${Math.max(8, Math.min(pointer.y + CURSOR_GAP, window.innerHeight - h - 8))}px`;
      return;
    }

    let top = anchor.belowTop;
    const spaceBelow = window.innerHeight - anchor.belowTop;
    if (spaceBelow < h + 8 && anchor.aboveBottom - h >= 8) {
      top = anchor.aboveBottom - h;
    }
    top = Math.max(8, Math.min(top, window.innerHeight - h - 8));

    const half = w / 2;
    let center = anchor.centerX;
    if (center - half < 8) center = half + 8;
    if (center + half > window.innerWidth - 8) center = window.innerWidth - 8 - half;

    el.style.left = `${center}px`;
    el.style.top = `${top}px`;
  }, [visible, anchor, popupOpen, guestBar, pointer]);

  const view = getEditorView();
  if (!visible || offscreen || !view || !(canEdit || canNoteOnly)) return null;
  // An artifact's source: markdown formatting would corrupt the markup, and an
  // artifact has no notes.
  if (isArtifactSourceView(view)) return null;

  // Same gate the action applies (chat access, host text only); hidden, not disabled.
  const quoteButton = canQuoteToChat() && isHostTextView(view) && (
    <button className="sel-toolbar-btn" onClick={() => { markdownActionRegistry.quoteToChat(view); hide(); }} title={t('quoteToChat')} aria-label={t('quoteToChat')}>
      <MessageSquareQuote size={ICON_SIZE} />
    </button>
  );

  // WHY: formatting writes through the view, so it is offered only on a view that takes
  // input — a full member's editor that is not editable yet (collab joining) or any more
  // (disconnected) gets the note-only bar, which never touches the text.
  if (!canEdit || !view.state.facet(EditorView.editable)) {
    // Panel quick preview: no notes on the previewed reference (decision A).
    if (isPanelPreviewView(view)) return null;
    if (guestBar) {
      return createPortal(
        <div
          ref={toolbarRef}
          data-testid="guest-note-bar"
          onMouseDown={(e: React.MouseEvent) => e.preventDefault()}
          className="fixed left-0 top-0 -translate-x-1/2 z-[45] shadow-lg"
        >
          <Button variant="yellow-strong" onClick={() => { markdownActionRegistry.createNote(view); hide(); }} title={t('noteTitle')}>
            <StickyNote size={ICON_SIZE} />
            {t('addNote')}
          </Button>
        </div>,
        document.body
      );
    }
    return createPortal(
      <div
        ref={toolbarRef}
        onMouseDown={(e: React.MouseEvent) => e.preventDefault()}
        className="selection-toolbar left-0 top-0 -translate-x-1/2"
      >
        <button className="sel-toolbar-btn gap-1.5 px-3" onClick={() => { markdownActionRegistry.createNote(view); hide(); }} title={t('noteTitle')}>
          <StickyNote size={ICON_SIZE} />
          <span>{t('addNote')}</span>
        </button>
        {quoteButton}
      </div>,
      document.body
    );
  }

  // WHY: the docked bar carries the same buttons; a second copy under the selection is noise.
  if (dockedOn) return null;

  /** After a formatting action: refresh styles/position, or hide if the selection collapsed. */
  const afterAction = () => {
    const { from, to } = view.state.selection.main;
    if (from === to) { hide(); return; }
    setStyles(detectActiveStyles(view));
    const a = computeAnchor(view);
    if (a) setAnchor(a);
  };

  return createPortal(
    <div
      ref={toolbarRef}
      onMouseDown={(e: React.MouseEvent) => e.preventDefault()}
      className="selection-toolbar left-0 top-0 -translate-x-1/2"
    >
      <FormattingButtons view={view} styles={styles} onAfterAction={afterAction} onSelectionAction={hide} onPopupChange={setPopupOpen} />
    </div>,
    document.body
  );
}

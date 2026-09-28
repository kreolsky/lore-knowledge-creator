/** Floating toolbar that appears on text selection for quick formatting. Store slices: selectionEmpty. Events consumed: show-link-suggestions, editor-selection-change. */

import type React from 'react';
import { useState, useEffect, useCallback, useLayoutEffect, useRef } from 'react';
import { createPortal } from 'react-dom';
import type { EditorView } from '@codemirror/view';
import { Bold, Italic, Strikethrough, TextQuote, Code, FileCode2, Link2, StickyNote, FileText, ChevronDown, List, ListOrdered, ListChecks, Bot } from 'lucide-react';
import { markdownActionRegistry, applyColorHighlight, getLastHighlightColor, createReference, HIGHLIGHT_COLORS } from './markdown-actions';
import { isPanelPreviewView } from '../../editor/editor-plugins';
import { agentAction } from './markdown-actions-agent';
import { useUIStore } from '../../store/ui-store';
import { useAppStore } from '../../store/app-store';
import { on, off } from '../../events';
import { useEditorView } from '../../editor/active-editor';
import { useTranslation } from '../../i18n';

interface ActiveStyles {
  headingLevel: number;
  bold: boolean;
  italic: boolean;
  strikethrough: boolean;
  inlineCode: boolean;
  quote: boolean;
  bullet: boolean;
  numbered: boolean;
  checkbox: boolean;
}

const STYLE_OPTIONS = [
  { labelKey: 'textStyle' as const, level: 0, hotkey: '\u2325\u2318 0' },
  { labelKey: 'heading1' as const, level: 1, hotkey: '\u2325\u2318 1' },
  { labelKey: 'heading2' as const, level: 2, hotkey: '\u2325\u2318 2' },
  { labelKey: 'heading3' as const, level: 3, hotkey: '\u2325\u2318 3' },
  { labelKey: 'heading4' as const, level: 4, hotkey: '\u2325\u2318 4' },
] as const;

const ICON_SIZE = 15;

function detectActiveStyles(view: EditorView): ActiveStyles {
  const { from, to } = view.state.selection.main;
  const line = view.state.doc.lineAt(from);

  const headingMatch = line.text.match(/^(#{1,4})\s/);
  const headingLevel = headingMatch ? headingMatch[1].length : 0;

  const selected = view.state.sliceDoc(from, to);
  const checkMarker = (marker: string): boolean => {
    const len = marker.length;
    if (selected.startsWith(marker) && selected.endsWith(marker) && selected.length >= len * 2) return true;
    const before = view.state.sliceDoc(Math.max(0, from - len), from);
    const after = view.state.sliceDoc(to, Math.min(view.state.doc.length, to + len));
    return before === marker && after === marker;
  };

  const endLine = view.state.doc.lineAt(to);
  let allQuoted = true;
  for (let i = line.number; i <= endLine.number; i++) {
    if (!view.state.doc.line(i).text.startsWith('> ')) { allQuoted = false; break; }
  }

  let allBullet = true;
  let allNumbered = true;
  let allCheckbox = true;
  for (let i = line.number; i <= endLine.number; i++) {
    const t = view.state.doc.line(i).text;
    if (t.length === 0) continue;
    if (!(t.startsWith('- ') || t.startsWith('* '))) allBullet = false;
    if (!(/^\d+\.\s/.test(t))) allNumbered = false;
    if (!(t.startsWith('- [ ] ') || t.startsWith('- [x] ') || t.startsWith('- [X] '))) allCheckbox = false;
  }

  return {
    headingLevel,
    bold: checkMarker('**'),
    italic: checkMarker('*') && !checkMarker('**'),
    strikethrough: checkMarker('~~'),
    inlineCode: checkMarker('`'),
    quote: allQuoted,
    bullet: allBullet,
    numbered: allNumbered,
    checkbox: allCheckbox,
  };
}

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

export default function SelectionToolbar() {
  const { t } = useTranslation();
  const getEditorView = useEditorView();
  const selectionEmpty = useUIStore(s => s.selectionEmpty);
  const accessLevel = useAppStore(s => s.accessLevel);
  // ARCH: full-access users get the formatting toolbar AND the Agent button.
  // Non-full users (commentator / viewer) see nothing — Agent is hidden too
  // per role walkthrough (cannot apply proposals anyway: backend returns 403).
  const canEdit = accessLevel === 'full';
  const [visible, setVisible] = useState(false);
  const [anchor, setAnchor] = useState<ToolbarAnchor>({ centerX: 0, belowTop: 0, aboveBottom: 0 });
  const [styles, setStyles] = useState<ActiveStyles>({ headingLevel: 0, bold: false, italic: false, strikethrough: false, inlineCode: false, quote: false, bullet: false, numbered: false, checkbox: false });
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [colorPickerOpen, setColorPickerOpen] = useState(false);
  const [lastColor, setLastColor] = useState(() => getLastHighlightColor());
  const toolbarRef = useRef<HTMLDivElement>(null);
  const mouseDownRef = useRef(false);
  const hasPendingRef = useRef(false);

  const hide = useCallback(() => {
    setVisible(false);
    setDropdownOpen(false);
    setColorPickerOpen(false);
    hasPendingRef.current = false;
  }, []);

  const showToolbar = useCallback(() => {
    const view = getEditorView();
    if (!view) return;
    const { from, to } = view.state.selection.main;
    if (from === to) { hide(); return; }
    const a = computeAnchor(view);
    if (!a) return;
    setAnchor(a);
    setStyles(detectActiveStyles(view));
    setVisible(true);
  }, [hide, getEditorView]);

  // WHY: getEditorView() is null at mount time (CM6 view is created async via
  // handleCreateEditor). We attach mouse listeners lazily on first selection event,
  // scoped to scrollDOM so sidebar/panel clicks don't interfere with pending state.
  const attachedDomRef = useRef<HTMLElement | null>(null);
  const attachMouseRef = useRef<() => void>(() => {});

  useEffect(() => {
    const onEditorMouseDown = () => { mouseDownRef.current = true; };
    const onEditorMouseUp = () => {
      mouseDownRef.current = false;
      if (hasPendingRef.current) {
        hasPendingRef.current = false;
        showToolbar();
      }
    };

    attachMouseRef.current = () => {
      const dom = getEditorView()?.scrollDOM;
      if (!dom || dom === attachedDomRef.current) return;
      attachedDomRef.current?.removeEventListener('mousedown', onEditorMouseDown);
      attachedDomRef.current?.removeEventListener('mouseup', onEditorMouseUp);
      dom.addEventListener('mousedown', onEditorMouseDown);
      dom.addEventListener('mouseup', onEditorMouseUp);
      attachedDomRef.current = dom;
    };

    const onLinkPopup = () => hide();
    on('show-link-suggestions', onLinkPopup);

    return () => {
      off('show-link-suggestions', onLinkPopup);
      attachedDomRef.current?.removeEventListener('mousedown', onEditorMouseDown);
      attachedDomRef.current?.removeEventListener('mouseup', onEditorMouseUp);
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
    const onSelChange = () => {
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

  // Escape to dismiss
  useEffect(() => {
    if (!visible) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') hide(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [visible, hide]);

  // Click outside toolbar to dismiss
  useEffect(() => {
    if (!visible) return;
    const onDown = (e: MouseEvent) => {
      if (toolbarRef.current && !toolbarRef.current.contains(e.target as Node)) hide();
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [visible, hide]);

  // ARCH: final position is decided post-measure in a layout effect so the toolbar
  // can flip above the selection when there's no room below, and clamp horizontally
  // by its actual width (transform: translateX(-50%) centers on `left`).
  useLayoutEffect(() => {
    const el = toolbarRef.current;
    if (!visible || !el) return;
    const w = el.offsetWidth;
    const h = el.offsetHeight;

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
  }, [visible, anchor, dropdownOpen, colorPickerOpen]);

  const view = getEditorView();
  if (!visible || !view || !canEdit) return null;

  /** Execute a formatting action, then refresh styles/position or hide if selection collapsed. */
  const executeAction = (action: keyof typeof markdownActionRegistry, closeDropdown = false) => {
    markdownActionRegistry[action](view);
    view.focus();
    if (closeDropdown) setDropdownOpen(false);
    setTimeout(() => {
      const { from, to } = view.state.selection.main;
      if (from !== to) {
        setStyles(detectActiveStyles(view));
        const a = computeAnchor(view);
        if (a) setAnchor(a);
      } else hide();
    }, 0);
  };

  const handleHeading = (level: number) => {
    const action = level === 0 ? 'clearFormatting' : `heading${level}` as keyof typeof markdownActionRegistry;
    executeAction(action, true);
  };

  const noFocus = (e: React.MouseEvent) => e.preventDefault();
  const currentStyleLabel = styles.headingLevel > 0 ? `H${styles.headingLevel}` : t('textStyle');

  return createPortal(
    <div
      ref={toolbarRef}
      onMouseDown={noFocus}
      className="selection-toolbar left-0 top-0 -translate-x-1/2"
    >
      {/* Style dropdown */}
      <div className="relative">
        <button className="sel-toolbar-btn gap-1 min-w-[52px]" onClick={() => { setDropdownOpen(!dropdownOpen); setColorPickerOpen(false); }}>
          <span className="font-semibold text-ui-base">{currentStyleLabel}</span>
          <ChevronDown size={12} />
        </button>
        {dropdownOpen && (
          <div className="sel-toolbar-dropdown">
            {STYLE_OPTIONS.map((opt) => {
              const isActive = styles.headingLevel === opt.level;
              return (
                <button
                  key={opt.level}
                  onClick={() => handleHeading(opt.level)}
                  style={{
                    fontWeight: opt.level > 0 ? 700 : 400,
                    fontSize: opt.level === 1 ? 17 : opt.level === 2 ? 15 : opt.level === 3 ? 14 : 13,
                  }}
                  className={`sel-toolbar-btn${isActive ? ' active' : ''} w-full py-1.5 px-3 justify-between`}
                >
                  <span>{opt.level > 0 && <span className="opacity-40 mr-2 text-ui-sm font-bold">H{opt.level}</span>}{t(opt.labelKey)}</span>
                  <span className="opacity-35 text-ui-xs font-[system-ui]">{opt.hotkey}</span>
                </button>
              );
            })}
          </div>
        )}
      </div>

      <div className="separator" />

      <div className="relative">
        <button
          className="sel-toolbar-btn"
          title={t('colorTitle')}
          aria-label={t('colorTitle')}
          onClick={() => {
            if (colorPickerOpen) {
              applyColorHighlight(view, lastColor);
              view.focus();
              setColorPickerOpen(false);
              setTimeout(() => {
                const { from, to } = view.state.selection.main;
                if (from !== to) {
                  setStyles(detectActiveStyles(view));
                  const a = computeAnchor(view);
                  if (a) setAnchor(a);
                } else hide();
              }, 0);
            } else {
              setDropdownOpen(false);
              setColorPickerOpen(true);
            }
          }}
        >
          <span
            className="block w-[15px] h-[15px]"
            style={{ backgroundColor: lastColor }}
          />
        </button>
        {colorPickerOpen && (
          <div className="sel-toolbar-color-picker">
            {HIGHLIGHT_COLORS.map((color) => (
              <button
                key={color}
                className={`sel-toolbar-color-swatch${color === lastColor ? ' active' : ''}`}
                style={{ backgroundColor: color }}
                aria-label={color}
                onClick={() => {
                  applyColorHighlight(view, color);
                  setLastColor(color);
                  view.focus();
                  setColorPickerOpen(false);
                  setTimeout(() => {
                    const { from, to } = view.state.selection.main;
                    if (from !== to) {
                      setStyles(detectActiveStyles(view));
                      const a = computeAnchor(view);
                      if (a) setAnchor(a);
                    } else hide();
                  }, 0);
                }}
              />
            ))}
          </div>
        )}
      </div>

      <div className="separator" />

      <button className={`sel-toolbar-btn${styles.bold ? ' active' : ''}`} onClick={() => executeAction('bold')} title={t('boldTitle')}>
        <Bold size={ICON_SIZE} />
      </button>
      <button className={`sel-toolbar-btn${styles.italic ? ' active' : ''}`} onClick={() => executeAction('italic')} title={t('italicTitle')}>
        <Italic size={ICON_SIZE} />
      </button>
      <button className={`sel-toolbar-btn${styles.strikethrough ? ' active' : ''}`} onClick={() => executeAction('strikethrough')} title={t('strikethroughTitle')}>
        <Strikethrough size={ICON_SIZE} />
      </button>

      <div className="separator" />

      <button className={`sel-toolbar-btn${styles.bullet ? ' active' : ''}`} onClick={() => executeAction('bulletList')} title={t('bulletListTitle')}>
        <List size={ICON_SIZE} />
      </button>
      <button className={`sel-toolbar-btn${styles.numbered ? ' active' : ''}`} onClick={() => executeAction('numberedList')} title={t('numberedListTitle')}>
        <ListOrdered size={ICON_SIZE} />
      </button>
      <button className={`sel-toolbar-btn${styles.checkbox ? ' active' : ''}`} onClick={() => executeAction('checkboxList')} title={t('checkboxListTitle')}>
        <ListChecks size={ICON_SIZE} />
      </button>

      <div className="separator" />

      <button className={`sel-toolbar-btn${styles.quote ? ' active' : ''}`} onClick={() => executeAction('quote')} title={t('quoteTitle')}>
        <TextQuote size={ICON_SIZE} />
      </button>
      <button className={`sel-toolbar-btn${styles.inlineCode ? ' active' : ''}`} onClick={() => executeAction('inlineCode')} title={t('inlineCodeTitle')}>
        <Code size={ICON_SIZE} />
      </button>
      <button className="sel-toolbar-btn" onClick={() => executeAction('codeBlock')} title={t('codeBlockTitle')}>
        <FileCode2 size={ICON_SIZE} />
      </button>

      <div className="separator" />

      <button className="sel-toolbar-btn" onClick={() => { executeAction('createLink'); hide(); }} title={t('linkTitle')}>
        <Link2 size={ICON_SIZE} />
      </button>
      {/* Panel quick preview: no notes on the previewed reference (decision A) —
          hide the button; the Mod-M keymap path toasts via createNote's gate. */}
      {!isPanelPreviewView(view) && (
        <button className="sel-toolbar-btn" onClick={() => { executeAction('createNote'); hide(); }} title={t('noteTitle')}>
          <StickyNote size={ICON_SIZE} />
        </button>
      )}
      <button className="sel-toolbar-btn" onClick={() => { createReference(view); hide(); }} title={t('createReferenceTitle')}>
        <FileText size={ICON_SIZE} />
      </button>

      <div className="separator" />

      {/* see SYSTEM: selection-region-agent — open a pinned-region agent chat for the
          selection. Full-access only (the toolbar is already canEdit-gated above). */}
      <button className="sel-toolbar-btn" onClick={() => { agentAction(view); hide(); }} title={t('selectionToolbarAgent')} aria-label={t('selectionToolbarAgent')}>
        <Bot size={ICON_SIZE} />
      </button>
    </div>,
    document.body
  );
}

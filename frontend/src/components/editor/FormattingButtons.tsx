/**
 * The formatting button row shared by the floating SelectionToolbar and the docked
 * DockedFormatToolbar: style menu, highlight colour, inline marks, lists, quote, code,
 * link, note, reference, quote-to-chat. Owns its own style-menu / colour-picker state;
 * the shell owns placement and what happens after an action.
 */

import type React from 'react';
import { useState, useEffect, useRef } from 'react';
import { EditorView } from '@codemirror/view';
import { Bold, Italic, Strikethrough, TextQuote, Code, FileCode2, Link2, StickyNote, FileText, ChevronDown, List, ListOrdered, ListChecks, MessageSquareQuote } from 'lucide-react';
import { markdownActionRegistry, applyColorHighlight, getLastHighlightColor, createReference, HIGHLIGHT_COLORS, canQuoteToChat, isHostTextView } from './markdown-actions';
import { isPanelPreviewView } from '../../editor/editor-plugins';
import { useTranslation } from '../../i18n';

export interface ActiveStyles {
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

export const NO_STYLES: ActiveStyles = { headingLevel: 0, bold: false, italic: false, strikethrough: false, inlineCode: false, quote: false, bullet: false, numbered: false, checkbox: false };

const STYLE_OPTIONS = [
  { labelKey: 'textStyle' as const, level: 0, hotkey: '⌥⌘ 0' },
  { labelKey: 'heading1' as const, level: 1, hotkey: '⌥⌘ 1' },
  { labelKey: 'heading2' as const, level: 2, hotkey: '⌥⌘ 2' },
  { labelKey: 'heading3' as const, level: 3, hotkey: '⌥⌘ 3' },
  { labelKey: 'heading4' as const, level: 4, hotkey: '⌥⌘ 4' },
] as const;

export const ICON_SIZE = 15;

export function detectActiveStyles(view: EditorView): ActiveStyles {
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

interface Props {
  /** The editor the buttons act on; null renders every button disabled. */
  view: EditorView | null;
  styles: ActiveStyles;
  /** Every button disabled (no editable view under the docked bar). */
  disabled?: boolean;
  /** After a formatting action ran (view already refocused). */
  onAfterAction: () => void;
  /** After link / note / reference / quote-to-chat — the floating bar hides on these. */
  onSelectionAction: () => void;
  /** Style menu or colour picker opened/closed — the floating bar re-measures. */
  onPopupChange?: (open: boolean) => void;
}

// WHY the disabled look: the docked bar keeps unusable buttons on screen; they must read as off.
const BTN = 'sel-toolbar-btn disabled:opacity-40 disabled:pointer-events-none';

const Separator = () => <div className="w-px self-stretch my-1 mx-[3px] bg-border" />;

export function FormattingButtons({ view, styles, disabled = false, onAfterAction, onSelectionAction, onPopupChange }: Props) {
  const { t } = useTranslation();
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [colorPickerOpen, setColorPickerOpen] = useState(false);
  const [lastColor, setLastColor] = useState(() => getLastHighlightColor());
  const styleRef = useRef<HTMLDivElement>(null);
  const colorRef = useRef<HTMLDivElement>(null);

  const popupOpen = dropdownOpen || colorPickerOpen;
  useEffect(() => { onPopupChange?.(popupOpen); }, [popupOpen, onPopupChange]);

  // WHY: the docked bar never hides, so an open menu must close on a press elsewhere.
  useEffect(() => {
    if (!popupOpen) return;
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (styleRef.current?.contains(target) || colorRef.current?.contains(target)) return;
      setDropdownOpen(false);
      setColorPickerOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [popupOpen]);

  const off = disabled || !view;
  const hasSelection = !!view && !view.state.selection.main.empty;

  const executeAction = (action: keyof typeof markdownActionRegistry) => {
    if (!view) return;
    markdownActionRegistry[action](view);
    view.focus();
    setDropdownOpen(false);
    setTimeout(onAfterAction, 0);
  };

  const applyColor = (color: string) => {
    if (!view) return;
    applyColorHighlight(view, color);
    setLastColor(color);
    view.focus();
    setColorPickerOpen(false);
    setTimeout(onAfterAction, 0);
  };

  const runSelectionAction = (run: (v: EditorView) => unknown, refocus = false) => {
    if (!view) return;
    run(view);
    if (refocus) view.focus();
    onSelectionAction();
  };

  const handleHeading = (level: number) => {
    executeAction(level === 0 ? 'clearFormatting' : `heading${level}` as keyof typeof markdownActionRegistry);
  };

  const toggle = (active: boolean, action: keyof typeof markdownActionRegistry, title: string, icon: React.ReactNode) => (
    <button className={`${BTN}${active ? ' active' : ''}`} disabled={off} aria-pressed={active} onClick={() => executeAction(action)} title={title} aria-label={title}>
      {icon}
    </button>
  );

  const currentStyleLabel = styles.headingLevel > 0 ? `H${styles.headingLevel}` : t('textStyle');

  return (
    <>
      <div ref={styleRef} className="relative">
        <button className={`${BTN} gap-1 min-w-[52px]`} disabled={off} onClick={() => { setDropdownOpen(!dropdownOpen); setColorPickerOpen(false); }}>
          {/* WHY the invisible twin: the cell is always as wide as the «Text» label, so
              switching to H1–H4 does not resize the button and shift the row. */}
          <span className="grid font-semibold text-ui-base">
            <span className="col-start-1 row-start-1 invisible" aria-hidden="true">{t('textStyle')}</span>
            <span className="col-start-1 row-start-1 text-center">{currentStyleLabel}</span>
          </span>
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
                  className={`${BTN}${isActive ? ' active' : ''} w-full py-1.5 px-3 justify-between`}
                >
                  <span>{opt.level > 0 && <span className="opacity-40 mr-2 text-ui-sm font-bold">H{opt.level}</span>}{t(opt.labelKey)}</span>
                  <span className="opacity-35 text-ui-xs font-[system-ui]">{opt.hotkey}</span>
                </button>
              );
            })}
          </div>
        )}
      </div>

      <Separator />

      <div ref={colorRef} className="relative">
        <button
          className={`${BTN}`}
          title={t('colorTitle')}
          aria-label={t('colorTitle')}
          disabled={off}
          onClick={() => {
            if (colorPickerOpen) { applyColor(lastColor); return; }
            setDropdownOpen(false);
            setColorPickerOpen(true);
          }}
        >
          <span className="block w-[15px] h-[15px]" style={{ backgroundColor: lastColor }} />
        </button>
        {colorPickerOpen && (
          <div className="sel-toolbar-color-picker">
            {HIGHLIGHT_COLORS.map((color) => (
              <button
                key={color}
                className={`sel-toolbar-color-swatch${color === lastColor ? ' active' : ''}`}
                style={{ backgroundColor: color }}
                aria-label={color}
                onClick={() => applyColor(color)}
              />
            ))}
          </div>
        )}
      </div>

      <Separator />

      {toggle(styles.bold, 'bold', t('boldTitle'), <Bold size={ICON_SIZE} />)}
      {toggle(styles.italic, 'italic', t('italicTitle'), <Italic size={ICON_SIZE} />)}
      {toggle(styles.strikethrough, 'strikethrough', t('strikethroughTitle'), <Strikethrough size={ICON_SIZE} />)}

      <Separator />

      {toggle(styles.bullet, 'bulletList', t('bulletListTitle'), <List size={ICON_SIZE} />)}
      {toggle(styles.numbered, 'numberedList', t('numberedListTitle'), <ListOrdered size={ICON_SIZE} />)}
      {toggle(styles.checkbox, 'checkboxList', t('checkboxListTitle'), <ListChecks size={ICON_SIZE} />)}

      <Separator />

      {toggle(styles.quote, 'quote', t('quoteTitle'), <TextQuote size={ICON_SIZE} />)}
      {toggle(styles.inlineCode, 'inlineCode', t('inlineCodeTitle'), <Code size={ICON_SIZE} />)}
      <button className={`${BTN}`} disabled={off} onClick={() => executeAction('codeBlock')} title={t('codeBlockTitle')} aria-label={t('codeBlockTitle')}>
        <FileCode2 size={ICON_SIZE} />
      </button>

      <Separator />

      <button className={`${BTN}`} disabled={off} onClick={() => runSelectionAction(markdownActionRegistry.createLink, true)} title={t('linkTitle')} aria-label={t('linkTitle')}>
        <Link2 size={ICON_SIZE} />
      </button>
      {/* Panel quick preview: no notes on the previewed reference — hide the button;
          the Mod-M keymap path toasts via createNote's gate. */}
      {!(view && isPanelPreviewView(view)) && (
        <button className={`${BTN}`} disabled={off || !hasSelection} onClick={() => runSelectionAction(markdownActionRegistry.createNote, true)} title={t('noteTitle')} aria-label={t('noteTitle')}>
          <StickyNote size={ICON_SIZE} />
        </button>
      )}
      <button className={`${BTN}`} disabled={off || !hasSelection} onClick={() => runSelectionAction(createReference)} title={t('createReferenceTitle')} aria-label={t('createReferenceTitle')}>
        <FileText size={ICON_SIZE} />
      </button>
      {/* Same gate the action applies (chat access, host text only); hidden, not disabled. */}
      {view && canQuoteToChat() && isHostTextView(view) && (
        <button className={`${BTN}`} disabled={off || !hasSelection} onClick={() => runSelectionAction(markdownActionRegistry.quoteToChat)} title={t('quoteToChat')} aria-label={t('quoteToChat')}>
          <MessageSquareQuote size={ICON_SIZE} />
        </button>
      )}
    </>
  );
}

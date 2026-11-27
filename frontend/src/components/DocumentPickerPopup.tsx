/**
 * Floating document picker: search + list sorted around an anchor document + preview.
 *
 * The shared body of the parent picker (ParentPickerPopup supplies its exclusions, the
 * "no parent" lead item and the reparent on select) and chat mode's document picker
 * (ChatModeDocHeader opens the picked document). Rendered via createPortal to document.body.
 */

import { useState, useEffect, useRef, useMemo, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { FieldInput } from './ui';
import { useTranslation } from '../i18n';
import { sortDocumentsForChat } from '../utils/document-sort';
import { buildParentMap } from '../utils/document-hierarchy';
import { useNavMode } from '../hooks/useNavMode';
import { useDocumentPreview } from '../hooks/useDocumentPreview';
import { usePopupSlot } from '../hooks/usePopupSlot';
import { useClickOutside } from '../hooks/useClickOutside';
import { PickerPreviewPopup } from './PickerPreviewPopup';
import { computeBesideBoxPreviewPosition } from '../utils/popup-position';
import { PREVIEW_PICKER_MAX_HEIGHT } from '../utils/preview-geometry';
import type { Document } from '../types';

const POPUP_WIDTH = 340;

interface Props {
  documents: Document[];
  /** Never offered (descendants of a moved doc, the Memory folder). */
  excludedIds?: Set<string>;
  /** An item above the documents; its id is '' (the parent picker's "no parent"). */
  leadLabel?: string;
  /** The row tagged "current". */
  currentId?: string | null;
  /** The document the list sorts around (proximity). */
  anchorDocId: string | null;
  onSelect: (id: string) => void;
  anchorRect: DOMRect;
  onClose: () => void;
  /** Open above anchor instead of below (default: by available space) */
  above?: boolean;
  /** Hide preview panel (default: false) */
  hidePreview?: boolean;
}

export function DocumentPickerPopup({ documents, excludedIds, leadLabel, currentId, anchorDocId, onSelect, anchorRect, onClose, above, hidePreview }: Props) {
  const { t } = useTranslation();
  // Mounted === wants open. Dominates hover previews so opening this hides them.
  const isActive = usePopupSlot('parent-picker', true);

  const [searchQuery, setSearchQuery] = useState('');
  const [selectedIndex, setSelectedIndex] = useState(0);

  const [previewDocId, setPreviewDocId] = useState<string | null>(null);
  const { content: rawContent, error: previewError, loading: previewLoading } = useDocumentPreview(previewDocId);
  const lastContentRef = useRef('');
  if (rawContent !== undefined) lastContentRef.current = rawContent;
  const previewContent = lastContentRef.current;
  const { isKeyboard, activateKeyboard } = useNavMode();

  const popupRef = useRef<HTMLDivElement>(null);
  const previewRef = useRef<HTMLDivElement>(null);
  const previewRootCallback = useCallback((el: HTMLDivElement | null) => {
    previewRef.current = el;
  }, []);

  const parentMap = useMemo(() => buildParentMap(documents), [documents]);

  const items = useMemo(() => {
    const q = searchQuery.toLowerCase();
    const filtered = documents.filter(d =>
      !d.is_index
      && !excludedIds?.has(d.document_id)
      && d.title.toLowerCase().includes(q),
    );
    const sorted = sortDocumentsForChat(filtered, anchorDocId, parentMap, searchQuery);
    const docs = sorted.map(d => ({ id: d.document_id, label: d.title }));
    // ARCH: the lead item's id is the empty string — the parent picker's backend treats "" as clearing parent_id
    const showLead = leadLabel !== undefined && (!q || leadLabel.toLowerCase().includes(q));
    return showLead ? [{ id: '', label: leadLabel }, ...docs] : docs;
  }, [searchQuery, documents, excludedIds, anchorDocId, leadLabel, parentMap]);

  useEffect(() => {
    setPreviewDocId(items.length > 0 && items[0].id ? items[0].id : null);
  }, [items]);

  const clampedIndex = Math.max(0, Math.min(selectedIndex, items.length - 1));

  useClickOutside([popupRef, previewRef], onClose);

  const handleArrow = (dir: 1 | -1) => {
    activateKeyboard();
    setSelectedIndex(i => {
      const next = dir === 1 ? Math.min(i + 1, items.length - 1) : Math.max(i - 1, 0);
      const item = items[next];
      setPreviewDocId(item && item.id ? item.id : null);
      return next;
    });
  };

  // Position popup below or above the anchor, clamped to viewport
  const popupLeft = Math.max(4, Math.min(anchorRect.left, window.innerWidth - POPUP_WIDTH - 12));
  const spaceBelow = window.innerHeight - anchorRect.bottom - 8;
  const spaceAbove = anchorRect.top - 8;
  // Sized to fit ~9 document rows (no tab header). Keep in sync with the inline maxHeight below.
  const POPUP_ESTIMATED_HEIGHT = 330;
  const openAbove = above ?? (spaceBelow < POPUP_ESTIMATED_HEIGHT && spaceAbove > spaceBelow);
  const popupTop = openAbove
    ? Math.max(4, anchorRect.top - POPUP_ESTIMATED_HEIGHT)
    : anchorRect.bottom + 4;

  // Preview panel on whichever side has more space
  const currentItem = items[clampedIndex];
  const previewPos = (!hidePreview && currentItem && currentItem.id)
    ? computeBesideBoxPreviewPosition(popupLeft, popupTop, POPUP_WIDTH, PREVIEW_PICKER_MAX_HEIGHT)
    : null;

  // Suppressed by a higher-priority popup — render nothing (see usePopupSlot contract).
  if (!isActive) return null;

  return createPortal(
    <>
      <div
        ref={popupRef}
        data-sidebar-hold
        className="link-suggest-popup"
        style={{ top: popupTop, left: popupLeft, maxHeight: POPUP_ESTIMATED_HEIGHT }}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="mb-1.5">
          <FieldInput
            autoFocus
            type="text"
            placeholder={t('searchDocuments')}
            value={searchQuery}
            onChange={(e) => { setSearchQuery(e.target.value); setSelectedIndex(0); }}
            className="w-full text-ui-base py-1.5 px-2.5"
            onKeyDown={(e) => {
              if (e.key === 'Escape') { onClose(); return; }
              if (e.key === 'ArrowDown') { e.preventDefault(); handleArrow(1); return; }
              if (e.key === 'ArrowUp') { e.preventDefault(); handleArrow(-1); return; }
              if (e.key === 'Enter') {
                e.preventDefault();
                if (items.length > 0) onSelect(items[clampedIndex].id);
              }
            }}
          />
        </div>
        <div className={`list-scroll overflow-y-auto flex-1${isKeyboard ? ' nav-keyboard' : ''}`}>
          {items.map((item, i) => (
            <button
              key={item.id || '__lead__'}
              onClick={() => onSelect(item.id)}
              className={`link-suggest-item${i === clampedIndex ? ' selected' : ''}${item.id === currentId ? ' current-parent' : ''}`}
              // INVARIANT: scroll-to-active only on keyboard nav, never on mouse hover/edge.
              // Why: hovering up from the trigger button scrolled the list and hid the first
              // pre-selected item; auto-scroll must require an explicit user action.
              ref={el => { if (i === clampedIndex && isKeyboard) el?.scrollIntoView({ block: 'nearest' }); }}
              onMouseEnter={() => {
                if (isKeyboard) return;
                setSelectedIndex(i);
                setPreviewDocId(item.id || null);
              }}
            >
              {!item.id && <span className="opacity-50 text-ui-xs mr-1.5">↑</span>}
              {item.label}
              {item.id === currentId && <span className="ml-auto text-ui-2xs px-1.5 py-0.5 bg-[var(--warm-tint)] text-text-muted">{t('current')}</span>}
            </button>
          ))}
          {items.length === 0 && (
            <div className="link-suggest-empty">{t('noDocumentsFound')}</div>
          )}
        </div>
      </div>
      {previewDocId && (
        <PickerPreviewPopup
          pos={previewPos}
          // WHY not useDocumentTitle: `documents` may be another project's tree (the
          // AccessPanel move target), absent from the store the hook reads.
          title={documents.find(d => d.document_id === previewDocId)?.title}
          content={previewContent}
          error={previewError}
          loading={previewLoading}
          popupRef={previewRootCallback}
        />
      )}
    </>,
    document.body,
  );
}

/**
 * Reusable plain-text content preview popup for hover over links and document tree items.
 *
 * Rendered via createPortal to document.body. Plain text with bottom fade gradient
 * (only when content overflows), or image display when imageUrl is provided.
 * Styling identical to ContentPickerPopup's preview panel.
 */
// SYSTEM: link-preview-popup — reusable hover preview panel (editor links, sidebar docs)

import { createPortal } from 'react-dom';
import { useState, useRef, useLayoutEffect, useEffect, useCallback } from 'react';
import { Loader2 } from 'lucide-react';
import { useTranslation } from '../i18n';
import { PREVIEW_WIDTH } from '../utils/preview-geometry';

interface LinkPreviewPopupProps {
  visible: boolean;
  top?: number;
  bottom?: number;
  left: number;
  maxHeight: number;
  content?: string;
  imageUrl?: string;
  /** The body fetch failed. Rendered as an error, never as an empty body. */
  error?: boolean;
  /** A body fetch is in flight. Passed explicitly — see the INVARIANT at the render site. */
  loading?: boolean;
  popupRef?: (el: HTMLDivElement | null) => void;
  onMouseLeave?: (e: React.MouseEvent) => void;
  onDismiss?: () => void;
}

export function LinkPreviewPopup({ visible, top, bottom, left, maxHeight, content, imageUrl, error, loading, popupRef, onMouseLeave, onDismiss }: LinkPreviewPopupProps) {
  if (!visible) return null;

  return createPortal(
    <LinkPreviewContent
      top={top}
      bottom={bottom}
      left={left}
      maxHeight={maxHeight}
      content={content}
      imageUrl={imageUrl}
      error={error}
      loading={loading}
      popupRef={popupRef}
      onMouseLeave={onMouseLeave}
      onDismiss={onDismiss}
    />,
    document.body,
  );
}

function LinkPreviewContent({
  top, bottom, left, maxHeight, content, imageUrl, error, loading, popupRef, onMouseLeave, onDismiss,
}: Omit<LinkPreviewPopupProps, 'visible'>) {
  const { t } = useTranslation();
  const textRef = useRef<HTMLDivElement | null>(null);
  const [overflows, setOverflows] = useState(false);

  useLayoutEffect(() => {
    const el = textRef.current;
    if (el) {
      setOverflows(el.scrollHeight > el.clientHeight);
    }
  }, [content, maxHeight]);

  const containerRef = useRef<HTMLDivElement | null>(null);

  const handleRef = useCallback((el: HTMLDivElement | null) => {
    containerRef.current = el;
    popupRef?.(el);
  }, [popupRef]);

  const onDismissRef = useRef(onDismiss);
  onDismissRef.current = onDismiss;

  useEffect(() => {
    if (!onDismissRef.current) return;

    const onMouseDown = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        onDismissRef.current?.();
      }
    };
    const onBlur = () => onDismissRef.current?.();

    document.addEventListener('mousedown', onMouseDown, true);
    window.addEventListener('blur', onBlur);
    return () => {
      document.removeEventListener('mousedown', onMouseDown, true);
      window.removeEventListener('blur', onBlur);
    };
  }, []);

  const style: React.CSSProperties = {
    left,
    width: PREVIEW_WIDTH,
    maxHeight,
    background: 'var(--surface)',
    boxShadow: '0 8px 32px rgba(0,0,0,0.25)',
  };
  if (bottom != null) style.bottom = bottom;
  else if (top != null) style.top = top;

  return (
    <div
      ref={handleRef}
      className="fixed z-55 flex flex-col p-3 overflow-hidden"
      style={style}
      onMouseLeave={onMouseLeave}
    >
      {imageUrl ? (
        <img
          src={imageUrl}
          alt=""
          className="w-full object-contain"
          style={{ maxHeight: maxHeight - 24 }}
        />
      ) : (
        <div ref={textRef} className="text-ui-sm text-[var(--text-muted)] leading-relaxed overflow-hidden flex-1 relative whitespace-pre-wrap">
          {/* INVARIANT(no-silent-degradation): error → loading → content → empty, in this
              order. A failed fetch reads as an error and a pending one as a spinner; only a
              body that actually arrived empty may read as "no content". Why: `content` is ''
              in all three cases, so `content || t('noContent')` reported a 404 and an
              in-flight GET as a genuinely-empty document. Empty state ≠ error state ≠
              loading state (mirrors PanelLoading). One GENERIC error message — never the
              status code: the backend returns a uniform 404 for missing AND forbidden
              (INVARIANT(security) in documents.py), so a per-status message would rebuild
              the existence oracle that invariant exists to prevent. */}
          {error ? (
            <span className="text-red">{t('previewLoadError')}</span>
          ) : loading ? (
            <span className="flex items-center gap-2"><Loader2 size={12} className="animate-spin" />{t('loading')}</span>
          ) : (
            content || t('noContent')
          )}
          {overflows && (
            <div
              className="absolute bottom-0 left-0 right-0 h-8 pointer-events-none bg-[linear-gradient(transparent,var(--surface))]"
            />
          )}
        </div>
      )}
    </div>
  );
}

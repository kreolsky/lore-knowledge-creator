/**
 * ARCH: Modal is the only UI component with rounded corners (rounded-[14px]),
 * intentional exception to the project-wide border-radius:0 rule.
 */
import { useEffect, useId, useRef, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { IconButton } from './IconButton';

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  width?: number;
  children: React.ReactNode;
  footer?: React.ReactNode;
  /** Accessible label for the close button. Pass t('close') from consumer. */
  closeLabel?: string;
  /**
   * Element to focus on open instead of the first focusable child. Why: by
   * default the first focusable is the header close (X) button; pass a ref to a
   * primary action so Enter confirms the intended (non-destructive) action.
   */
  focusRef?: RefObject<HTMLElement | null>;
}

const FOCUSABLE = 'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])';

export function Modal({ open, onClose, title, width = 420, children, footer, closeLabel = 'Close', focusRef }: ModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const previousFocusRef = useRef<Element | null>(null);
  const titleId = useId();

  useEffect(() => {
    if (!open) return;
    previousFocusRef.current = document.activeElement;
    const dialog = dialogRef.current;
    const focusTarget = focusRef?.current
      ?? dialog?.querySelector<HTMLElement>('textarea:not([disabled]),input:not([disabled])');
    if (focusTarget) {
      focusTarget.focus();
    } else {
      const first = dialog?.querySelector<HTMLElement>(FOCUSABLE);
      first?.focus();
    }
    return () => {
      (previousFocusRef.current as HTMLElement)?.focus?.();
    };
    // focusRef is intentionally excluded: a stable ref's .current updates without
    // re-running this effect; the dialog re-mounts via the `open` toggle.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Escape') { onClose(); return; }
    if (e.key !== 'Tab') return;
    const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  return createPortal(
    <div
      className="fixed inset-0 bg-black/35 z-[200] flex items-center justify-center"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      onKeyDown={handleKeyDown}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="bg-surface border border-border rounded-[14px] max-w-[calc(100vw-32px)] shadow-[0_24px_64px_rgba(0,0,0,0.3)] flex flex-col overflow-hidden"
        style={{ width }}
      >
        <div className="flex items-center py-4 px-5 border-b border-border-soft gap-2">
          <span id={titleId} className="text-sm font-semibold text-text flex-1">{title}</span>
          <IconButton onClick={onClose} aria-label={closeLabel}><X size={15} /></IconButton>
        </div>
        <div className="p-5 flex flex-col gap-4">
          {children}
        </div>
        {footer && (
          <div className="py-3 px-5 border-t border-border-soft flex justify-end gap-2">
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body
  );
}

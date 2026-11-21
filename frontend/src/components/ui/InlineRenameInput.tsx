/**
 * Inline rename box shared by every title-in-place editor: Enter and blur commit, Escape cancels.
 * Visuals belong to the caller's rename class (.doc-rename-input / .ref-rename-input / …).
 */

import { forwardRef, useRef } from 'react';
import type { CSSProperties } from 'react';

interface InlineRenameInputProps {
  value: string;
  onChange: (value: string) => void;
  // `via` lets a caller act on Enter only (Breadcrumb hands focus back to the editor).
  onCommit: (via: 'enter' | 'blur') => void;
  onCancel: () => void;
  className?: string;
  autoFocus?: boolean;
  selectOnFocus?: boolean;
  // Runtime-computed sizing only (Breadcrumb's ch-width).
  style?: CSSProperties;
}

export const InlineRenameInput = forwardRef<HTMLInputElement, InlineRenameInputProps>(
  function InlineRenameInput({ value, onChange, onCommit, onCancel, className, autoFocus = true, selectOnFocus, style }, ref) {
    // WHY: a site may still be mounted when the browser fires blur after Escape or Enter
    // (DocumentTree and ChatHeader keep the input focused until the PATCH returns); without
    // the flag that blur would commit the abandoned value, or send the same rename twice.
    // Any edit re-arms blur: after a failed PATCH the input stays open and a new value is pending.
    const settledRef = useRef(false);
    return (
      <input
        ref={ref}
        className={className}
        style={style}
        data-rename-input
        value={value}
        autoFocus={autoFocus}
        onChange={e => { settledRef.current = false; onChange(e.target.value); }}
        onFocus={e => { settledRef.current = false; if (selectOnFocus) e.currentTarget.select(); }}
        onKeyDown={e => {
          e.stopPropagation();
          if (e.key === 'Enter') { e.preventDefault(); settledRef.current = true; onCommit('enter'); }
          if (e.key === 'Escape') { settledRef.current = true; onCancel(); }
        }}
        onBlur={() => { if (!settledRef.current) onCommit('blur'); }}
        onClick={e => e.stopPropagation()}
      />
    );
  },
);

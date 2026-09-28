/**
 * ARCH: className accepts ONLY layout utilities (width, flex, margin).
 * Visual style (bg, border, color, font) is locked in the component.
 * Label is NOT part of the component — use composition (separate <span>/<label>).
 */

import { forwardRef } from 'react';
import type { ChangeEventHandler, KeyboardEventHandler, FocusEventHandler, MouseEventHandler, ClipboardEventHandler } from 'react';

interface FieldInputProps {
  className?: string;
  value?: string;
  defaultValue?: string;
  onChange?: ChangeEventHandler<HTMLInputElement>;
  onKeyDown?: KeyboardEventHandler<HTMLInputElement>;
  onBlur?: FocusEventHandler<HTMLInputElement>;
  onClick?: MouseEventHandler<HTMLInputElement>;
  placeholder?: string;
  disabled?: boolean;
  readOnly?: boolean;
  type?: string;
  autoFocus?: boolean;
  autoComplete?: string;
  name?: string;
  maxLength?: number;
  inputMode?: 'none' | 'text' | 'decimal' | 'numeric' | 'tel' | 'search' | 'email' | 'url';
}

interface FieldTextareaProps {
  className?: string;
  // Swaps the locked neutral surface for sticky-yellow (AI zero-chat experiment).
  highlight?: boolean;
  // Monospace, smaller text — for code-like values (a pasted JSON workflow).
  mono?: boolean;
  value?: string;
  defaultValue?: string;
  onChange?: ChangeEventHandler<HTMLTextAreaElement>;
  onKeyDown?: KeyboardEventHandler<HTMLTextAreaElement>;
  onPaste?: ClipboardEventHandler<HTMLTextAreaElement>;
  placeholder?: string;
  disabled?: boolean;
  readOnly?: boolean;
  rows?: number;
  autoFocus?: boolean;
  name?: string;
}

const inputCls =
  'py-2 px-3 border border-border bg-surface2 text-text font-inherit text-sm outline-none transition-[border-color] duration-150 focus:border-border focus:shadow-none';

const textareaCls =
  `${inputCls} resize-y min-h-[80px] leading-[1.55]`;

export const FieldInput = forwardRef<HTMLInputElement, FieldInputProps>(
  function FieldInput({ className, ...props }, ref) {
    // h-[37px] pins the fractional box (font: inherit → 37.1px) so a Button
    // size="lg" beside it (same 37px) lines up exactly. Inputs only — the
    // textarea keeps its content-driven height.
    return <input ref={ref} className={`${inputCls} h-[37px]${className ? ` ${className}` : ' w-full'}`} {...props} />;
  },
);

export const FieldTextarea = forwardRef<HTMLTextAreaElement, FieldTextareaProps>(
  function FieldTextarea({ className, highlight, mono, ...props }, ref) {
    const bg = highlight
      ? ' !bg-[var(--sticky-yellow-light)] !border-[var(--sticky-yellow-sep)]'
      : '';
    const font = mono ? ' font-mono !text-xs' : '';
    return <textarea ref={ref} className={`${textareaCls}${bg}${font}${className ? ` ${className}` : ' w-full'}`} {...props} />;
  },
);

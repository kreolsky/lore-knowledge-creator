/** Four-slot PIN input shared by the app lock, the page lock and the remove-page-lock modal. */

import type React from 'react';
import { useState } from 'react';

export function PinDigitsInput({ value, onChange, onSubmit, ariaLabel, inputRef }: {
  value: string;
  onChange: (pin: string) => void;
  onSubmit: () => void;
  ariaLabel: string;
  inputRef: React.RefObject<HTMLInputElement | null>;
}) {
  const [focused, setFocused] = useState(false);
  return (
    // WHY: the real input is invisible and stretched over four fixed slots, so
    // each digit lands exactly on its dot and the caret can only sit on the next
    // empty slot — a free-text input centred its own glyphs between the dots.
    <div className="relative w-[176px] h-14 border border-border bg-surface2 flex items-center">
      {[0, 1, 2, 3].map(i => (
        <div key={i} className="flex-1 flex items-center justify-center text-3xl text-text">
          {value[i] ?? (focused && i === value.length
            ? <span className="w-px h-8 bg-text animate-pulse" />
            : <span className="text-text-dim">•</span>)}
        </div>
      ))}
      <input
        ref={inputRef}
        className="absolute inset-0 w-full h-full opacity-0 cursor-text text-base"
        aria-label={ariaLabel}
        value={value}
        onChange={e => onChange(e.target.value.replace(/\D/g, '').slice(0, 4))}
        onKeyDown={e => { if (e.key === 'Enter' && value.length === 4) onSubmit(); }}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        inputMode="numeric"
        maxLength={4}
        autoComplete="off"
      />
    </div>
  );
}

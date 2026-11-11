/** Color picker field: a native swatch (hex) beside a read-only hex code, square like every field. */
import { toHex } from '../../theme/color-math';

interface ColorFieldProps {
  /** Any CSS color; shown and picked as #rrggbb. */
  value: string;
  onChange: (hex: string) => void;
  'aria-label'?: string;
}

export function ColorField({ value, onChange, 'aria-label': ariaLabel }: ColorFieldProps) {
  const hex = toHex(value);
  return (
    <div className="flex items-center gap-2 h-[30px] px-2 border border-border bg-surface2">
      <input
        type="color"
        value={hex}
        onChange={e => onChange(e.target.value)}
        aria-label={ariaLabel}
        className="w-6 h-6 p-0 border border-border bg-transparent cursor-pointer appearance-none [&::-webkit-color-swatch-wrapper]:p-0 [&::-webkit-color-swatch]:border-none [&::-webkit-color-swatch]:rounded-none [&::-moz-color-swatch]:border-none [&::-moz-color-swatch]:rounded-none"
      />
      <span className="text-sm font-mono text-text-muted">{hex}</span>
    </div>
  );
}

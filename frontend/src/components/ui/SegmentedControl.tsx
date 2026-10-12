/** Segmented control — a row of mutually exclusive stops; exactly one is active (radiogroup). */

interface SegmentedOption {
  value: string;
  label: string;
  disabled?: boolean;
}

interface SegmentedControlProps {
  options: SegmentedOption[];
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  ariaLabel: string;
}

export function SegmentedControl({ options, value, onChange, disabled, ariaLabel }: SegmentedControlProps) {
  return (
    <div role="radiogroup" aria-label={ariaLabel} className="inline-flex border border-border">
      {options.map(opt => {
        const active = opt.value === value;
        const off = disabled || opt.disabled;
        return (
          <button
            key={opt.value}
            type="button"
            role="radio"
            aria-checked={active}
            disabled={off}
            onClick={() => { if (!active) onChange(opt.value); }}
            className={`px-2.5 py-1 text-ui-xs whitespace-nowrap transition-colors duration-150 ${
              active ? 'bg-accent text-bg' : 'bg-surface text-text-dim hover:bg-surface2'
            } ${off ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}`}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

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
            // h-[20px] stop + the container's 1px borders = 22px total — the
            // SAME height as Button/Dropdown size="sm", so a Dropdown sharing
            // a row with this control (AudienceSection) aligns exactly;
            // the fixed height also makes it font-independent.
            className={`h-[20px] px-2.5 inline-flex items-center text-ui-xs whitespace-nowrap transition-colors duration-150 ${
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

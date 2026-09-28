/** Checkbox wrapper — keeps raw <input type="checkbox"> inside the ui/ exempt zone. */

interface FieldCheckboxProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  className?: string;
  /** Native tooltip on the whole control (checkbox + label). */
  title?: string;
  /** Disabled (greyed, non-interactive) — used for dependent controls like the
   *  Include-subtree checkbox that only act once the parent Share box is on. */
  disabled?: boolean;
}

export function FieldCheckbox({ checked, onChange, label, className, title, disabled }: FieldCheckboxProps) {
  return (
    <label
      title={title}
      className={`flex items-center gap-1.5 select-none ${disabled ? 'cursor-default opacity-40' : 'cursor-pointer'}${className ? ` ${className}` : ''}`}
    >
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={e => onChange(e.target.checked)}
        className="accent-accent"
      />
      {label}
    </label>
  );
}

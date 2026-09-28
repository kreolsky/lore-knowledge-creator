/** Radio input wrapper — keeps raw <input type="radio"> inside the ui/ exempt zone. */

interface FieldRadioProps {
  name: string;
  checked: boolean;
  onChange: () => void;
  label: string;
  description?: string;
}

export function FieldRadio({ name, checked, onChange, label, description }: FieldRadioProps) {
  return (
    <label className="flex items-start gap-2 py-1.5 cursor-pointer">
      <input
        type="radio"
        name={name}
        checked={checked}
        onChange={onChange}
        className="mt-0.5 accent-accent"
      />
      <div>
        {/* WHY: no size class — the label inherits the surrounding font exactly
            like FieldCheckbox's, so radios and checkboxes in one panel match. */}
        <span className="text-text">{label}</span>
        {description && (
          <p className="text-ui-xs text-text-dim mt-0.5">{description}</p>
        )}
      </div>
    </label>
  );
}

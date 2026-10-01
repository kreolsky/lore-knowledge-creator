/** Two-row project form (name + description) shared by the Dashboard create
 * panel and the card edit Modal. Row 1: name input + Cancel + submit, all at
 * the input's pinned 37px (Button size="lg"); row 2: optional description. */
// ARCH: Extracted from Dashboard.tsx — one form, two callers (create panel,
// edit modal); the create panel and the modal must never drift apart.

import type React from 'react';
import { useState } from 'react';
import { Button, FieldInput, FieldTextarea } from '../components/ui';
import { useTranslation } from '../i18n';

interface ProjectFormProps {
  initialName: string;
  initialDescription: string;
  submitLabel: string;
  onSubmit: (name: string, description: string | null) => void;
  onCancel: () => void;
}

/** Empty/whitespace-only description is normalised to null before sending, so
 * PATCH {description: null} clears the field (the backend maps null → NONE). */
function normalizeDescription(raw: string): string | null {
  const trimmed = raw.trim();
  return trimmed ? trimmed : null;
}

export function ProjectForm({ initialName, initialDescription, submitLabel, onSubmit, onCancel }: ProjectFormProps) {
  const { t } = useTranslation();
  const [name, setName] = useState(initialName);
  const [description, setDescription] = useState(initialDescription);

  // Same name rule as the removed inline rename: trimmed, >= 2 chars.
  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || trimmed.length < 2) return;
    onSubmit(trimmed, normalizeDescription(description));
  };

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-2">
      <div className="flex gap-2">
        <FieldInput
          autoFocus
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={t('projectPlaceholder')}
          className="flex-1"
        />
        <Button type="button" variant="ghost" size="lg" onClick={onCancel}>
          {t('cancel')}
        </Button>
        <Button type="submit" variant="primary" size="lg">
          {submitLabel}
        </Button>
      </div>
      <FieldTextarea
        rows={4}
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        placeholder={t('projectDescriptionPlaceholder')}
      />
    </form>
  );
}

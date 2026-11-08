/** Pure derived-state hooks for ChatInput, extracted.

Each hook wraps only useMemo/plain computations over its inputs — NO effect timing,
NO ref capture, NO render-tree change. ChatInput calls them and consumes the same
values the inline code computed, so behavior is identical (verified via tsc + the
existing send-gate / attachment-size unit tests that cover the underlying pure fns).
The presentational JSX split (topControls / leftControls / warnings) is the plan's
designated separate task — it needs interactive UI verification. */
import { useMemo } from 'react';

import type { Document } from '../../types';
import { totalAttachmentBytes } from '../../utils/attachment-size';

/**
 * Personas ≡ system prompts:
 * a persona is any DIRECT child document of the Personas folder (the reserved doc
 * whose system_role === 'system_prompt'), identified by parent_id — NOT by a role
 * tag. Plain docs (no is_system/role) added there are selectable personas.
 */
export function useSystemPrompts(
  documents: Document[],
  effectiveSystemPromptId: string | null,
): {
  personasFolderId: string | null;
  systemPrompts: Document[];
  activeSystemPromptTitle: string | undefined;
} {
  const personasFolderId = useMemo(
    () => documents.find(d => d.system_role === 'system_prompt')?.document_id ?? null,
    [documents],
  );
  const systemPrompts = useMemo(
    () => personasFolderId
      ? documents.filter(d => d.parent_id === personasFolderId)
      : [],
    [documents, personasFolderId],
  );
  const activeSystemPromptTitle = useMemo(
    () => effectiveSystemPromptId ? systemPrompts.find(p => p.document_id === effectiveSystemPromptId)?.title : undefined,
    [effectiveSystemPromptId, systemPrompts],
  );
  return { personasFolderId, systemPrompts, activeSystemPromptTitle };
}

/**
 * Shared attachment-budget math over the turn's PRESSED images. Only the new turn
 * counts: the wire carries the tail turn's images alone (see the INVARIANT in
 * completionBody).
 */
export function useAttachmentBudget(
  maxAttachmentMb: number,
  images: string[],
): {
  projectedAttachmentBytes: number;
  attachmentsOverLimit: boolean;
} {
  const projectedAttachmentBytes = totalAttachmentBytes(images);
  return {
    projectedAttachmentBytes,
    attachmentsOverLimit: projectedAttachmentBytes > maxAttachmentMb * 1024 * 1024,
  };
}

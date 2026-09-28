/** Pure derived-state hooks for ChatInput, extracted.

Each hook wraps only useMemo/plain computations over its inputs — NO effect timing,
NO ref capture, NO render-tree change. ChatInput calls them and consumes the same
values the inline code computed, so behavior is identical (verified via tsc + the
existing send-gate / attachment-size unit tests that cover the underlying pure fns).
The presentational JSX split (topControls / leftControls / warnings) is the plan's
designated separate task — it needs interactive UI verification. */
import { useMemo } from 'react';

import type { Document, ChatMessage } from '../../types';
import {
  totalAttachmentBytes,
  historyAttachmentBytes,
} from '../../utils/attachment-size';

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
 * Shared attachment-budget math. The budget is the WHOLE projected body's
 * image sum (history that rides along in apiMessages + new pending), NOT just the
 * new turn's images — the middleware caps the full request body, and every send
 * builder ships all non-deleted history images as LLM context.
 *
 * `contextOverLimit`: conversation history alone already exceeds the budget —
 * removing the new image can't fix it; the user must start a fresh chat. Distinct
 * from `attachmentsOverLimit` (new attachments too big) so the message + remedy differ.
 */
export function useAttachmentBudget(
  maxAttachmentMb: number,
  historyMessages: ChatMessage[],
  images: string[],
): {
  maxAttachmentBytes: number;
  historyBytes: number;
  pendingBytes: number;
  projectedAttachmentBytes: number;
  contextOverLimit: boolean;
  attachmentsOverLimit: boolean;
} {
  const maxAttachmentBytes = maxAttachmentMb * 1024 * 1024;
  const historyBytes = useMemo(() => historyAttachmentBytes(historyMessages), [historyMessages]);
  const pendingBytes = totalAttachmentBytes(images);
  const projectedAttachmentBytes = historyBytes + pendingBytes;
  const contextOverLimit = historyBytes > maxAttachmentBytes;
  const attachmentsOverLimit = !contextOverLimit && projectedAttachmentBytes > maxAttachmentBytes;
  return { maxAttachmentBytes, historyBytes, pendingBytes, projectedAttachmentBytes, contextOverLimit, attachmentsOverLimit };
}

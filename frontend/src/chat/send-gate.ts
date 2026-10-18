/** Plan "unify-agent-config": the chat empty-send gate (pure, unit-tested).

 * A selected persona ALONE permits an empty user message — a persona is a
 * complete instruction and the agent always knows the working document (the
 * `# Current document` metadata section is attached to every doc-scoped turn).
 * Document *content* is opt-in, but that does not block a persona-only turn. The
 * legacy `&& hasContext` clause is dropped. Images always permit an empty send.
 * The attachment-budget caps still gate regardless. */

export function canSendWithoutText(
  imagesCount: number,
  systemPromptId: string | null,
): boolean {
  // INVARIANT (plan "unify-agent-config"): persona set → empty allowed. Why: a
  // persona is a complete instruction and the working document's metadata is
  // always attached to a doc-scoped turn, so the user's text is redundant;
  // requiring attached content would block the common "pick a persona + an open
  // document" flow.
  return imagesCount > 0 || systemPromptId != null;
}

export interface CanSendInput {
  text: string;
  imagesCount: number;
  /** Uploaded file chips — they permit an empty send like images do. */
  filesCount?: number;
  /** A file chip still uploading holds the send: its path does not exist yet. */
  filesUploading?: boolean;
  systemPromptId: string | null;
  overLimit: boolean;
  contextOverLimit: boolean;
}

export function canSend(input: CanSendInput): boolean {
  const { text, imagesCount, filesCount = 0, filesUploading = false, systemPromptId, overLimit, contextOverLimit } = input;
  const withoutText = canSendWithoutText(imagesCount + filesCount, systemPromptId);
  return (text.trim().length > 0 || withoutText) && !overLimit && !contextOverLimit && !filesUploading;
}

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
  systemPromptId: string | null;
  overLimit: boolean;
  contextOverLimit: boolean;
}

export function canSend(input: CanSendInput): boolean {
  const { text, imagesCount, systemPromptId, overLimit, contextOverLimit } = input;
  const withoutText = canSendWithoutText(imagesCount, systemPromptId);
  return (text.trim().length > 0 || withoutText) && !overLimit && !contextOverLimit;
}

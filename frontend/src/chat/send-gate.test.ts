/** Plan "unify-agent-config": the empty-send gate.

 * A selected persona ALONE permits an empty user message (persona is a complete
 * instruction and context is always attached) — the legacy `&& hasContext`
 * clause is dropped. Images always permit empty send. Attachment-budget caps
 * still gate regardless. */

import { describe, it, expect } from 'vitest';
import { canSendWithoutText, canSend } from './send-gate';

describe('canSendWithoutText', () => {
  it('permits empty send when a persona is selected, even with no context', () => {
    expect(canSendWithoutText(0, 'p1')).toBe(true);
  });
  it('permits empty send when images are attached', () => {
    expect(canSendWithoutText(2, null)).toBe(true);
  });
  it('forbids empty send with no persona and no images', () => {
    expect(canSendWithoutText(0, null)).toBe(false);
  });
});

describe('canSend', () => {
  it('persona selected, empty text, no images → true', () => {
    expect(canSend({ text: '', imagesCount: 0, systemPromptId: 'p1', overLimit: false, contextOverLimit: false })).toBe(true);
  });
  it('no persona, empty text, no images → false', () => {
    expect(canSend({ text: '   ', imagesCount: 0, systemPromptId: null, overLimit: false, contextOverLimit: false })).toBe(false);
  });
  it('no persona, text present → true', () => {
    expect(canSend({ text: 'hi', imagesCount: 0, systemPromptId: null, overLimit: false, contextOverLimit: false })).toBe(true);
  });
  it('persona selected but attachments over budget → false', () => {
    expect(canSend({ text: '', imagesCount: 0, systemPromptId: 'p1', overLimit: true, contextOverLimit: false })).toBe(false);
  });
  it('history alone over budget → false even with text', () => {
    expect(canSend({ text: 'hi', imagesCount: 0, systemPromptId: null, overLimit: false, contextOverLimit: true })).toBe(false);
  });
});

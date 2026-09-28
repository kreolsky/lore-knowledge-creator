import { describe, it, expect } from 'vitest';

// effectiveCap resolves the gauge cap: the /models context_windows map entry for
// the model, else the 128k fallback.
import { effectiveCap, CHAT_CONTEXT_WINDOW_FALLBACK } from './misc-slice';

describe('effectiveCap', () => {
  it('uses the /models map value when present (primary source)', () => {
    const cw = { 'deepseek/flash': 262144, 'local/orange/chat': 131072 };
    expect(effectiveCap(cw, 'deepseek/flash')).toBe(262144);
    expect(effectiveCap(cw, 'local/orange/chat')).toBe(131072);
  });

  it('falls back to the 128k constant when the model is not in the map', () => {
    expect(effectiveCap({}, 'gemini/chat')).toBe(CHAT_CONTEXT_WINDOW_FALLBACK);
    expect(effectiveCap({}, undefined)).toBe(CHAT_CONTEXT_WINDOW_FALLBACK);
  });
});

/**
 * Chat cache reset registry.
 *
 * Mirrors logout-handlers.ts: each module-level mutable cache self-registers a
 * clear() here, and misc-slice.reset() fires them all via clearChatCaches(). This
 * test pins the registry contract: every handler runs, a throwing handler is
 * isolated (never rethrows, never skips the rest), dedupe is by reference, and
 * handlers persist across clearChatCaches() calls.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { registerChatResetHandler, clearChatCaches, __resetRegistryForTest } from './reset-registry';

describe('reset-registry (chat cache reset)', () => {
  beforeEach(() => {
    __resetRegistryForTest();
  });

  it('runs every registered handler', () => {
    const calls: string[] = [];
    registerChatResetHandler(() => calls.push('a'));
    registerChatResetHandler(() => calls.push('b'));
    clearChatCaches();
    expect(calls).toEqual(['a', 'b']);
  });

  it('isolates a throwing handler (never rethrows, never skips the rest)', () => {
    const calls: string[] = [];
    registerChatResetHandler(() => calls.push('before'));
    registerChatResetHandler(() => { throw new Error('boom'); });
    registerChatResetHandler(() => calls.push('after'));
    expect(() => clearChatCaches()).not.toThrow();
    expect(calls).toEqual(['before', 'after']);
  });

  it('dedupes handlers by reference', () => {
    let count = 0;
    const fn = (): void => { count += 1; };
    registerChatResetHandler(fn);
    registerChatResetHandler(fn); // same reference ⇒ registered once
    clearChatCaches();
    expect(count).toBe(1);
  });

  it('persists handlers across clearChatCaches() calls (registration is not consumed)', () => {
    let count = 0;
    registerChatResetHandler(() => { count += 1; });
    clearChatCaches();
    clearChatCaches();
    expect(count).toBe(2);
  });
});

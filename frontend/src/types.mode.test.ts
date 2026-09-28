/**
 * Tests for the single-AI-line mode derivation (agent_auto only).
 *
 * Pins the new contract after the Ask-line `mode` axis leaves the wire:
 *   - `ChatMode` + `normalizeMode` are deleted (no export).
 *   - `ChatUIMode` loses `'readonly'`; the surviving selector is `agent_auto`.
 *   - `deriveUIMode` reads ONLY `agent_auto` (the `mode` read is gone).
 *   - `inverseUIMode` returns ONLY `{ agentAuto }` (the `mode` half is gone).
 *
 * These fail against the current (pre-removal) code, then turn green.
 */
import { describe, it, expect } from 'vitest';

import { deriveUIMode, inverseUIMode } from './types';

describe('remove-ask-line-mode-axis — deriveUIMode (D6)', () => {
  it('derives agent_auto from agent_auto=true alone (no mode read)', () => {
    expect(deriveUIMode({ agent_auto: true })).toBe('agent_auto');
  });

  it('derives agent_confirm from agent_auto=false alone (no mode read)', () => {
    expect(deriveUIMode({ agent_auto: false })).toBe('agent_confirm');
  });

  it('ignores any legacy `mode` value — every AI chat is an agent chat', () => {
    // A legacy row that still carried `mode` (now removed from the type) must NOT
    // downgrade the result; agent_auto drives it alone. Cast through unknown to
    // simulate the legacy shape the type no longer admits.
    const legacy = (o: object) => o as unknown as Parameters<typeof deriveUIMode>[0];
    expect(deriveUIMode(legacy({ mode: 'ask', agent_auto: true }))).toBe('agent_auto');
    expect(deriveUIMode(legacy({ mode: 'chat', agent_auto: false }))).toBe('agent_confirm');
    expect(deriveUIMode(legacy({ mode: undefined, agent_auto: false }))).toBe('agent_confirm');
  });

  it('never returns "readonly" from any session shape', () => {
    expect(deriveUIMode(null)).toBe('agent_confirm');
    expect(deriveUIMode(undefined)).toBe('agent_confirm');
    expect(deriveUIMode({})).toBe('agent_confirm');
  });
});

describe('remove-ask-line-mode-axis — inverseUIMode (D6)', () => {
  it('returns ONLY agentAuto (no mode key on the wire)', () => {
    expect(inverseUIMode('agent_confirm')).toEqual({ agentAuto: false });
    expect(inverseUIMode('agent_auto')).toEqual({ agentAuto: true });
  });

  it('never puts a `mode` key on the returned object', () => {
    for (const m of ['agent_confirm', 'agent_auto'] as const) {
      const out = inverseUIMode(m) as Record<string, unknown>;
      expect('mode' in out).toBe(false);
    }
  });
});

describe('remove-ask-line-mode-axis — deleted exports (D6)', () => {
  it('normalizeMode is no longer exported', async () => {
    // Dynamic import returns the module namespace; normalizeMode must be absent.
    const mod = await import('./types');
    expect((mod as Record<string, unknown>).normalizeMode).toBeUndefined();
    expect((mod as Record<string, unknown>).ChatMode).toBeUndefined();
  });
});

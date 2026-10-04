/** setPinLocked reports the lock to telemetry — the trigger next to a provider `disconnect`. */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../telemetry/telemetry', () => ({ sendTelemetry: vi.fn() }));

import { sendTelemetry } from '../../telemetry/telemetry';
import { useAppStore } from '../app-store';

const sent = sendTelemetry as unknown as ReturnType<typeof vi.fn>;

describe('setPinLocked telemetry', () => {
  beforeEach(() => sent.mockClear());

  it('sends a pin-lock row carrying the new state, and applies it', () => {
    useAppStore.getState().setPinLocked(true);
    expect(sent).toHaveBeenCalledWith({ category: 'collab', kind: 'pin-lock', detail: { locked: true } });
    expect(useAppStore.getState().pinLocked).toBe(true);

    useAppStore.getState().setPinLocked(false);
    expect(sent).toHaveBeenLastCalledWith({ category: 'collab', kind: 'pin-lock', detail: { locked: false } });
  });
});

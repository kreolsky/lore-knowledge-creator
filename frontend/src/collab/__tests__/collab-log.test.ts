/** Tests for the collab telemetry producer. */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../telemetry/telemetry', () => ({ sendTelemetry: vi.fn() }));

import { sendTelemetry } from '../../telemetry/telemetry';
import { logCollabEvent } from '../collab-log';

const sent = sendTelemetry as unknown as ReturnType<typeof vi.fn>;

describe('logCollabEvent', () => {
  beforeEach(() => sent.mockClear());

  it('produces a category:collab event with code/reason and conn', () => {
    logCollabEvent('yjs', 'close', 'doc-1', {
      code: 4008, reason: 'Heartbeat timeout', wasClean: false, isAuth: false,
    });
    expect(sent).toHaveBeenCalledTimes(1);
    const ev = sent.mock.calls[0][0];
    expect(ev.category).toBe('collab');
    expect(ev.kind).toBe('close');
    expect(ev.entity_id).toBe('doc-1');
    expect(ev.detail).toMatchObject({ conn: 'yjs', code: 4008, reason: 'Heartbeat timeout' });
  });

  it('carries outage duration on open', () => {
    logCollabEvent('project', 'open', undefined, { outageMs: 1234 });
    expect(sent.mock.calls[0][0].detail).toMatchObject({ conn: 'project', outageMs: 1234 });
  });
});

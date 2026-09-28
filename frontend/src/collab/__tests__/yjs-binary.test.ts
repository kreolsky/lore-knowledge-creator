/** Tests for the collab binary frame codec (yjs-binary) — wrap/unwrap roundtrip + malformed frames. */
import { describe, it, expect } from 'vitest';
import { MSG_AWARENESS, MSG_SYNC, wrapBinary, unwrapBinary } from '../yjs-binary';

describe('yjs-binary codec', () => {
  it('roundtrips a sync frame (id + payload preserved)', () => {
    const payload = new Uint8Array([1, 2, 3, 250]);
    const wrapped = wrapBinary('doc-123', MSG_SYNC, payload);
    const unwrapped = unwrapBinary(wrapped.buffer as ArrayBuffer);
    expect(unwrapped).not.toBeNull();
    expect(unwrapped!.entityId).toBe('doc-123');
    expect(unwrapped!.msgType).toBe(MSG_SYNC);
    expect(Array.from(unwrapped!.payload)).toEqual([1, 2, 3, 250]);
  });

  it('roundtrips an awareness frame', () => {
    const payload = new Uint8Array([9]);
    const wrapped = wrapBinary('e1', MSG_AWARENESS, payload);
    const unwrapped = unwrapBinary(wrapped.buffer as ArrayBuffer);
    expect(unwrapped!.msgType).toBe(MSG_AWARENESS);
  });

  it('supports entity ids longer than 255 bytes (two-byte length)', () => {
    const longId = 'x'.repeat(300);
    const wrapped = wrapBinary(longId, MSG_SYNC, new Uint8Array([7]));
    const unwrapped = unwrapBinary(wrapped.buffer as ArrayBuffer);
    expect(unwrapped!.entityId).toBe(longId);
  });

  it('supports multibyte entity ids', () => {
    const id = 'док-Ω';
    const wrapped = wrapBinary(id, MSG_SYNC, new Uint8Array());
    const unwrapped = unwrapBinary(wrapped.buffer as ArrayBuffer);
    expect(unwrapped!.entityId).toBe(id);
  });

  it('returns null for malformed frames (short / truncated)', () => {
    expect(unwrapBinary(new Uint8Array(2).buffer as ArrayBuffer)).toBeNull();
    // Declares a 10-byte eid but carries only 2.
    const truncated = new Uint8Array([MSG_SYNC, 0, 10, 65, 66]);
    expect(unwrapBinary(truncated.buffer as ArrayBuffer)).toBeNull();
  });
});

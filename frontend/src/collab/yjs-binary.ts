/**
 * Binary frame codec for the multiplexed collab WebSocket.
 *
 * Frame layout: [msgType(1) | eidLenHi(1) | eidLenLo(1) | entityId(UTF-8) | payload].
 * Entity ids longer than 255 bytes use the two-byte length; the payload is
 * opaque (Yjs sync or awareness bytes).
 */
// ARCH: Binary frames carry Yjs sync/awareness with entity_id envelope.
//       JSON frames carry control (join/leave/flush/heartbeat) — see yjs-provider.

export const MSG_SYNC = 0;
export const MSG_AWARENESS = 1;

export const SYNC_STEP1 = 0;
export const SYNC_STEP2 = 1;
export const SYNC_UPDATE = 2;

export function wrapBinary(entityId: string, msgType: number, payload: Uint8Array): Uint8Array<ArrayBuffer> {
  const eidBytes = new TextEncoder().encode(entityId);
  const result = new Uint8Array(3 + eidBytes.length + payload.length);
  result[0] = msgType;
  result[1] = (eidBytes.length >> 8) & 0xff;
  result[2] = eidBytes.length & 0xff;
  result.set(eidBytes, 3);
  result.set(payload, 3 + eidBytes.length);
  return result;
}

export function unwrapBinary(data: ArrayBuffer): { entityId: string; msgType: number; payload: Uint8Array } | null {
  const bytes = new Uint8Array(data);
  if (bytes.length < 3) return null;
  const msgType = bytes[0];
  const eidLen = (bytes[1] << 8) | bytes[2];
  if (bytes.length < 3 + eidLen) return null;
  const entityId = new TextDecoder().decode(bytes.slice(3, 3 + eidLen));
  const payload = bytes.slice(3 + eidLen);
  return { entityId, msgType, payload };
}

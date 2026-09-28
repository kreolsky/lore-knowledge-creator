"""Yjs sync protocol handler — binary sync step1/step2/update + awareness.

Implements the y-protocols/sync wire format for server-side Y.Doc sync.

Wire format (y-protocols):
  Byte 0: message type
    0 = sync message
      Byte 1: sync type
        0 = step 1 (state vector)
        1 = step 2 (state diff)
        2 = update
    1 = awareness
      Byte 1: awareness type
        1 = awareness update

# SYSTEM: collab-sync — Yjs sync protocol for CRDT Y.Doc synchronization
# ARCH: Server holds a full Y.Doc replica per entity. Clients sync via
#       step1/step2 on connect, then exchange incremental updates.
"""

from __future__ import annotations

import logging
import struct

from pycrdt import Doc

logger = logging.getLogger(__name__)

MSG_SYNC = 0
MSG_SYNC_STEP1 = 0
MSG_SYNC_STEP2 = 1
MSG_SYNC_UPDATE = 2

MSG_AWARENESS = 1
MSG_AWARENESS_UPDATE = 1


def handle_sync_message(doc: Doc, message: bytes) -> tuple[bytes | None, bytes | None]:
    """Process a Yjs sync message and return (response, update_to_broadcast).

    Returns:
        response: bytes to send back to the sender (step2 reply for step1, None otherwise)
        update_to_broadcast: bytes to relay to other clients (new update applied to doc)

    # ARCH: The server applies all incoming updates to its local Y.Doc replica.
    #       For step1, it computes the diff and sends step2 back.
    #       For step2/update, it applies the payload and flags it for broadcast.
    """
    if len(message) < 2:
        return None, None

    msg_type = message[0]

    if msg_type == MSG_SYNC:
        return _handle_sync(doc, message)
    # INVARIANT(corruption): awareness is never a doc update — return (None, None) so it is never
    # persisted/applied. Relay happens in CollabSession.handle_binary_message instead.
    # Why: apply_update() on awareness bytes raises and poisons the ydoc_updates log.
    return None, None


def _handle_sync(doc: Doc, message: bytes) -> tuple[bytes | None, bytes | None]:
    if len(message) < 3:
        return None, None

    sync_type = message[1]
    payload = message[2:]

    if sync_type == MSG_SYNC_STEP1:
        remote_state = payload
        diff = doc.get_update(remote_state)
        response = bytes([MSG_SYNC, MSG_SYNC_STEP2]) + diff
        return response, None

    elif sync_type == MSG_SYNC_STEP2:
        if payload:
            try:
                doc.apply_update(payload)
            except Exception:
                logger.warning("Malformed sync step2 payload — dropped")
                return None, None
        return None, payload if payload else None

    elif sync_type == MSG_SYNC_UPDATE:
        if payload:
            try:
                doc.apply_update(payload)
            except Exception:
                logger.warning("Malformed sync update payload — dropped")
                return None, None
        return None, payload

    return None, None


def create_sync_step1_message(state_vector: bytes) -> bytes:
    return bytes([MSG_SYNC, MSG_SYNC_STEP1]) + state_vector


def create_sync_step2_message(update: bytes) -> bytes:
    return bytes([MSG_SYNC, MSG_SYNC_STEP2]) + update


def create_update_message(update: bytes) -> bytes:
    return bytes([MSG_SYNC, MSG_SYNC_UPDATE]) + update


def wrap_binary(entity_id: str, msg_type: int, payload: bytes) -> bytes:
    """Wrap a Yjs message with entity_id for the multiplexed project WS.

    Envelope format:
      Byte 0: protocol message type (0=sync, 1=awareness)
      Bytes 1-2: entity_id length (uint16 big-endian)
      Bytes 3..3+N: entity_id (UTF-8)
      Remaining: payload
    """
    entity_bytes = entity_id.encode("utf-8")
    return struct.pack(">BH", msg_type, len(entity_bytes)) + entity_bytes + payload


def unwrap_binary(data: bytes) -> tuple[str, int, bytes] | None:
    """Unwrap a multiplexed binary frame. Returns (entity_id, msg_type, payload) or None."""
    if len(data) < 3:
        return None
    msg_type = data[0]
    eid_len = struct.unpack(">H", data[1:3])[0]
    if len(data) < 3 + eid_len:
        return None
    entity_id = data[3:3 + eid_len].decode("utf-8")
    payload = data[3 + eid_len:]
    return entity_id, msg_type, payload

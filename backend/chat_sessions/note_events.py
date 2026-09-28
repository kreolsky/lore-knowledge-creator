"""Note-session realtime nudges: the broadcast target and fire-and-forget scheduling."""

import asyncio

from chat_sessions.serialize import build_ref_map
from models import is_ref_row


# ARCH: resolve the broadcast
# entity_id to the VIEWED (parent) document so a note anchored to a reference-doc
# reaches the collab session that users have actually joined. A note's raw
# chat_sessions.document_id may point to a reference-doc (notes are created with
# document_id = reference_id); nobody opens the reference in the editor, they view
# its parent — so we MUST broadcast to the parent's doc session. Edge cases (a note
# visible from a sibling/ancestor per the list_sessions scope union) are reconciled
# on the next loadSessions; the broadcast covers the primary audience.
async def broadcast_doc_id(db, session: dict) -> str:
    """Resolve the parent document_id that the note's realtime frame targets."""
    did = session.get("document_id")
    if did:
        ref_map = await build_ref_map(db, did)
        if is_ref_row(ref_map.get(did) or {}):
            parent = ref_map[did].get("parent_id")
            if parent:
                return parent
    return did


# WHY: best-effort realtime nudge for note
# message mutations. Genuinely fire-and-forget — the resolve+emit runs as a
# tracked background task so the HTTP response returns immediately; the DB
# resolution (broadcast_doc_id + _session_preview) is NOT awaited on the request
# path. Why: note mutations are user-facing; awaiting 2+ extra DB round-trips per
# create/edit/delete regressed perceived latency. Correctness does not depend on
# synchronous delivery — receivers reconcile on the next loadSessions,
# and event_bus.emit itself fans out via its own _spawn-tracked tasks.
_emit_tasks: set[asyncio.Task] = set()


def fire_note_emit(coro) -> None:
    """Schedule a fire-and-forget note emit, holding a strong ref so it is not GC'd."""
    task = asyncio.create_task(coro)
    _emit_tasks.add(task)
    task.add_done_callback(_emit_tasks.discard)

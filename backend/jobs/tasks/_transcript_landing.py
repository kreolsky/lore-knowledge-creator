"""The STT landing tail — ONE home for what happens after transcribe_audio returns.

# Both STT tasks (media.transcribe_task, widget_extract._stt_phase) land the
# transcript here — the single place the success tail lives. Each caller keeps its own failure
# recorder and its own pre-STT status write — only the SUCCESS tail is shared.
"""
from __future__ import annotations

import event_bus
from models import is_ref_row


async def land_transcript(
    reference_id: str, transcription: str, *,
    user_id: str | None = None, emit_complete: bool = True,
) -> bool:
    """Land a finished transcript on its reference row; False = ref vanished.

    Re-reads the ref (it may have been deleted mid-STT — then nothing is
    written), writes via set_content and emits content_flushed. The NAMED
    divergence is ``emit_complete``: transcribe_task emits
    transcription_complete — the wet hook's trigger (on_transcription_complete
    enqueues extract_task, which CREATES a child document) — while the widget
    extract path passes False: its contract is DRY, and firing the wet hook
    would produce the exact document that surface promises not to make.
    """
    from db import fetch_one
    from ydoc_store import set_content

    ref = await fetch_one("documents", reference_id)
    if not ref or ref.get("deleted_at") or not is_ref_row(ref):
        return False

    # INVARIANT(corruption): Worker has no live collab session — must use set_content which
    # persists AND publishes the Yjs update to the backplane ydoc:{id} channel.
    # Why: the worker holds no editors; a plain DB UPDATE would leave every open editor
    # showing stale text. set_content is the only path that writes Surreal AND fans the
    # update to the web replica, whose live CollabSession broadcasts to editors.
    await set_content(reference_id, transcription, persist=True,
                      extra_sets={"processing_status": "ready"})
    # set_content does NOT emit content_flushed (it is also called mid-flush by
    # paths that emit their own), so a worker write would never schedule an embed. Emit
    # here, once per worker write, the way _finalize_content_mutation does for the REST
    # path — embeddings._on_content_flushed (subscribed eagerly) then debounces.
    # Not "exactly once" globally: with a live editor open, the web replica applies this
    # write off the backplane, marks the session dirty and its flush emits its own
    # content_flushed. The trailing debounce collapses the pair into one embed, so the
    # duplicate is harmless — but do not add a second emit here on that assumption.
    await event_bus.emit("content_flushed", entity_type="doc", entity_id=reference_id,
                         project_id=ref.get("project_id", ""))
    if emit_complete:
        await event_bus.emit("transcription_complete",
                             reference_id=reference_id,
                             project_id=ref.get("project_id", ""),
                             user_id=user_id)
    return True

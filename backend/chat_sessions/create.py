"""Create a chat session: row fields, LLM-config inheritance, the note-created nudge."""

import logging
from uuid import uuid4

import settings

import event_bus
from chat_sessions.note_events import broadcast_doc_id, fire_note_emit
from chat_sessions.serialize import build_session_ref_map, serialize_session
from db import create_record, fetch_one
from models import SessionCreate

logger = logging.getLogger(__name__)


# ARCH: LLM-config inheritance is
# PROJECT-LATEST per field. Each omitted field is read from the single latest AI
# chat in the project (ORDER BY updated_at DESC LIMIT 1, is_note=false). Both
# `model` and `system_prompt_id` therefore come from the SAME latest row; the old
# per-branch Reference→parent-Document fall-through is gone (it could not survive
# project-wide listing — there is no branch to walk). An explicit value (including
# `null`/None = "Default / no prompt") still overrides inheritance; only a truly
# omitted field (not in model_fields_set) inherits.
_INHERITED_FIELDS = ("model", "system_prompt_id")


async def _latest_chat_field(db, field: str, *, project_id: str, user_id: str) -> str | None:
    """Return the value of `field` from the latest AI chat in the project.

    Project-latest: drops the document_id branch filter and adds
    `is_note = false` so a note session (which carries model='') can never donate
    to a new AI chat. Why updated_at is the recency key here (not last-message
    time): this picks which chat DONATES model/system_prompt — nothing user-
    visible — and updated_at is not bumped on the AI completion path, so the donor
    is "latest settings/creation" not "latest activity". Accepted limitation.
    """
    # WHY include updated_at in SELECT: SurrealDB requires ORDER BY fields be in projection.
    rows = await db.query(
        f"SELECT {field}, updated_at FROM chat_sessions "
        "WHERE project_id = $pid AND user_id = $uid "
        "AND is_note = false AND deleted_at IS NONE "
        "ORDER BY updated_at DESC LIMIT 1",
        {"pid": project_id, "uid": user_id},
    )
    if not rows:
        return None
    val = rows[0].get(field)
    return val if val else None


async def _resolve_inherited(db, body: SessionCreate, user_id: str) -> dict:
    """Resolve missing inheritance fields from the project-latest AI chat.

    If body.parent_session_id is provided AND resolves to a session owned by the
    same user in the same project (and not soft-deleted), inheritance fields are
    pulled from that session, overriding the project-latest walk per field.
    """
    out: dict = {}
    parent_session: dict | None = None
    if body.parent_session_id:
        candidate = await fetch_one("chat_sessions", body.parent_session_id)
        if (
            candidate
            and candidate.get("user_id") == user_id
            and candidate.get("project_id") == body.project_id
            and candidate.get("deleted_at") is None
        ):
            parent_session = candidate
    for field in _INHERITED_FIELDS:
        if field in body.model_fields_set:
            out[field] = getattr(body, field)
            continue
        if parent_session is not None:
            parent_val = parent_session.get(field)
            out[field] = parent_val if parent_val else None
            continue
        out[field] = await _latest_chat_field(
            db, field, project_id=body.project_id, user_id=user_id,
        )
    # Final fallback for `model` only — system prompts default to None.
    if not out.get("model"):
        out["model"] = await settings.get("CHAT_MODEL")
    return out


def _note_fields(body: SessionCreate) -> dict:
    """The note-only columns: the flag and the stored anchor."""
    data: dict = {"is_note": True}
    if body.anchor_offset_start is not None and body.anchor_offset_end is not None:
        data["anchor_offset_start"] = body.anchor_offset_start
        data["anchor_offset_end"] = body.anchor_offset_end
        # INVARIANT: anchor_rel_* is a presence-only hint today — it marks a note
        # as "anchored" but is NOT decoded back into a position anywhere yet (the
        # [text](note:ID) link in the body is the source of truth for the highlight). Why:
        # live note creation stores a Yjs RelativePosition JSON string (from the
        # frontend), while the one-shot migration backfills a pycrdt StickyIndex
        # (binary). The two encodings are NOT byte-comparable — reconcile them before
        # any code tries to resolve anchor_rel_* into an offset.
        if body.anchor_rel_start and body.anchor_rel_end:
            data["anchor_rel_start"] = body.anchor_rel_start.encode("utf-8")
            data["anchor_rel_end"] = body.anchor_rel_end.encode("utf-8")
    return data


async def _ai_chat_fields(db, body: SessionCreate, user_id: str) -> dict:
    """The AI-chat columns: inherited LLM config plus the agent create fields."""
    inherited = await _resolve_inherited(db, body, user_id)
    data: dict = {"model": inherited["model"]}
    if inherited.get("system_prompt_id"):
        data["system_prompt_id"] = inherited["system_prompt_id"]
    # ARCH: the agent create fields are
    # UNCONDITIONAL for non-note sessions (every AI chat is an agent chat).
    # target_doc_id was defaulted in SessionCreate._check_invariants to
    # reference_id or document_id when the client omitted it. agent_auto persists
    # the "Full auto" dropdown choice; has_region persists the pinned-
    # region flag. Notes are kept out of this
    # block by the `not body.is_note` guard (the `else` arm in
    # create_session_command) — the ONLY thing preventing these
    # fields landing on a note row now that the model-validator's is_note+agent
    # rejection is gone.
    data["target_doc_id"] = body.target_doc_id
    data["agent_auto"] = bool(body.agent_auto)
    data["has_region"] = bool(body.has_region)
    # ARCH: stored when explicitly set at
    # create; omitted → column absent (reads as null = Default). Validated
    # at PATCH time only — create trusts the caller exactly like `model`
    # does (an invalid pin dies at turn time with the router's explicit
    # 400, never silently).
    if body.reasoning_effort is not None:
        data["reasoning_effort"] = body.reasoning_effort
    return data


async def create_session_command(db, body: SessionCreate, user_id: str) -> dict:
    """Write the session row and return it serialized; the caller has gated access.

    Note-chat semantics (body.is_note=true): LLM inheritance is skipped (notes
    never call the LLM) and anchor_offset_* are stored as-is in code points.
    """
    uid = str(uuid4())
    data: dict = {
        "project_id": body.project_id,
        "user_id": user_id,
        "title": "",
        "model": "",
    }
    if body.reference_id:
        # Legacy alias — the reference is itself a document now.
        data["document_id"] = body.reference_id
    elif body.document_id:
        data["document_id"] = body.document_id
    if body.is_note:
        data.update(_note_fields(body))
    else:
        data.update(await _ai_chat_fields(db, body, user_id))
        # ARCH: every AI chat is a THREAD — the
        # root row's own id is the thread id, and active_branch_id (the
        # last-opened branch, what the chat list previews) starts at itself.
        # Notes are out of scope: their reply tree is a different product and
        # keeps no thread columns.
        data["thread_id"] = uid
        data["active_branch_id"] = uid
    row = await create_record("chat_sessions", uid, data)
    out = serialize_session(row, await build_session_ref_map(db, data),
                            viewer_id=user_id)
    if body.is_note:
        # WHY: realtime nudge for a freshly-created
        # note. Fire-and-forget (background task) so create latency is unaffected; the
        # actor's own client also receives the frame and dedups by session_id. Broadcast
        # entity_id is the resolved parent document.
        fire_note_emit(_emit_note_created(db, row, out))
    return out


async def _emit_note_created(db, row: dict, out: dict) -> None:
    try:
        eid = await broadcast_doc_id(db, row)
        await event_bus.emit(
            "note_session_created",
            entity_type="doc", entity_id=eid,
            event={"type": "note_session_created", "session": out},
        )
    except Exception:
        logger.warning("note_session_created emit failed", exc_info=True)

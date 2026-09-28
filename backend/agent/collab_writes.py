"""Agent-driven create + checkpoint + presence helpers (shared by Tool-API + proposals).

Part of the chat-agent-mode system: the collab-backed create paths
(create_document_via_collab / create_reference_via_collab) and the shared
pre-edit checkpoint + presence broadcast. Internal extraction — no SYSTEM marker;
this module OWNS its names.
"""
import logging
from uuid import uuid4

import event_bus
from agent import doc_state

logger = logging.getLogger(__name__)


async def _create_agent_pre_edit_checkpoint(
    *,
    document_id: str,
    content: str,
    tables_json: str | None,
    original_preview: str,
) -> dict | None:
    """Snapshot the document's pre-edit state before applying an agent rewrite.

    # WHY: called only after _splice_edit's stale check passes, i.e.
    # Why: unconditional checkpointing after the stale-gate guarantees every committed mutation has history.
    # after we've committed to mutating the document. Unconditional — produces a
    # checkpoint regardless of doc/ref type or change size. The History panel
    # listens for the `checkpoint_created` event and prepends it live.

    # ARCH: routes through the unified writer (cp_store.create_checkpoint) so the
    # agent-auto checkpoint now carries content_ref + content_hash + tables state —
    # fixing the pre-existing gap where it skipped the blob + integrity hash entirely.
    """
    from auto_backup import LABEL_AGENT_AUTO
    from cp_store import create_checkpoint as _create_checkpoint

    # DEBT: audit granted auto-applies by key_id on the CHECKPOINT row — Why deferred:
    # the checkpoint is created here with `created_by=None`; threading the agent
    # key_id onto it would require (a) a new `agent_key_id` field on the checkpoints
    # table + the cp_store shared-field builder (the checkpoint_row_fields INVARIANT
    # consumed by all 7 creation paths), or (b) overloading `created_by`/`label`
    # (semantic regression in the History UI). Both cross the cp_store shared
    # invariant for an ergonomics plan. The acting key_id IS recorded on the
    # agent-tool telemetry row (tool_api_telemetry.build_detail) — that durable-enough
    # system record is the audit source-of-truth for granted auto-applies (Slice 3).
    # Residual owner: operator/UI — surface key_id in the checkpoint History when the
    # cp_store field addition is scoped.
    checkpoint = await _create_checkpoint(
        document_id=document_id,
        content=content,
        tables_json=tables_json,
        label=LABEL_AGENT_AUTO,
        comment=f"Before agent edit: '{original_preview}'",
        created_by=None,
    )
    await _emit_checkpoint_created(document_id, checkpoint)
    return checkpoint


async def _emit_checkpoint_created(document_id: str, checkpoint: dict) -> None:
    """Broadcast `checkpoint_created` so the History panel prepends the entry live.

    # Why: the live History list listens on this event; serializing (json_safe) and
    # emitting are one cohesive broadcast concern, split out of creation (workflow.md
    # hard rule: Function > 50 lines → split).
    """
    from deps import json_safe

    await event_bus.emit(
        "checkpoint_created",
        entity_type="doc",
        entity_id=document_id,
        event={"type": "checkpoint_created", "checkpoint": json_safe(checkpoint)},
    )


async def broadcast_agent_presence(doc_id: str, user_id: str | None) -> None:
    """Fire the decorative agent-editing presence signal.

    Wrapped so a decorative broadcast failure never fails an apply — presence is
    best-effort and must never gate the mutation.
    """
    try:
        from collab.events import broadcast_agent_editing

        await broadcast_agent_editing(doc_id, on_behalf_of=user_id)
    except Exception:
        logger.debug("agent presence broadcast failed for %s", doc_id, exc_info=True)


async def create_document_via_collab(
    *,
    title: str,
    content: str,
    parent_id: str | None,
    project_id: str,
    user: dict,
    scope_root: str | None = None,
    is_system: bool = False,
) -> dict:
    """Single source of truth for agent-driven document creation (PR4 R3).

    `is_system` marks the row as reserved system infrastructure (grouped in the
    tree apart from user documents).

    Validates title + content cap, normalizes list spacing, validates the parent,
    derives a unique path (preserving the sort_key invariant), creates the row,
    and emits `document_created` with sort_key so the client tree inserts
    correctly. Both the Tool-API direct path (`_apply_create_document_direct`)
    and the chat proposal apply path (`_apply_create_proposal`) delegate here, so
    the two create entry points can never drift on invariants.

    # ARCH: when `scope_root` is set, a missing
    # parent_id defaults to the subtree root (the new doc lands INSIDE the wall),
    # and any explicit parent must already be in the subtree (else 403). Unscoped
    # keys keep today's behavior (parent_id as-passed).

    Returns {"doc_id": ..., "sort_key": ...}. Raises HTTPException on validation
    failure. Caller is responsible for the access check (the two callers use
    different access-resolution helpers) and any proposal bookkeeping.
    """
    from documents.service import assert_parent_valid, create_with_unique_path
    from fastapi import HTTPException
    from markdown_normalize import normalize_list_spacing, unwrap_placeholder_brackets

    from config import MAX_PROPOSAL_NEW_TEXT_CHARS
    from scope import require_doc_in_scope, resolve_scoped_parent

    title = (title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    if len(content) > MAX_PROPOSAL_NEW_TEXT_CHARS:
        raise HTTPException(status_code=413, detail="Content too large")
    # Reject remote image URLs in authored text (degradation — no server-side
    # fetch; name the fix). Shared with the edit/append convergence path.
    doc_state.reject_remote_images(content)
    # Unwrap `(<id>)` placeholder-bracket destinations — one of the three agent
    # write sites for the unwrap (create).
    content = unwrap_placeholder_brackets(normalize_list_spacing(content))
    # T5: under a scoped key, a missing parent defaults to the subtree root and an
    # explicit parent must lie inside the subtree (the wall cannot be bypassed by
    # choosing an out-of-scope parent).
    parent_id = resolve_scoped_parent(scope_root, parent_id)
    if parent_id:
        await require_doc_in_scope(scope_root, parent_id)
    await assert_parent_valid(parent_id, project_id)

    doc_id = str(uuid4())
    payload = {
        "project_id": project_id, "parent_id": parent_id,
        "title": title, "content": content, "is_reference": False,
        "is_system": is_system,
    }
    try:
        await create_with_unique_path(
            doc_id, payload, project_id=project_id, title=title,
            user_id=user.get("user_id"), user_name=user.get("name"),
        )
    except Exception:
        logger.exception("Agent create_document failed (project=%s)", project_id)
        raise HTTPException(status_code=500, detail="Failed to create document")

    await event_bus.emit(
        "document_created",
        project_id=project_id, document_id=doc_id, title=title,
        parent_id=parent_id, is_reference=False,
        sort_key=payload.get("sort_key"),
    )
    # WHY: a create that CARRIES a body runs the same finalize tail as an edit —
    # mentions rebuilt, `content_flushed` emitted.
    # Why: the embedding debounce and the mention graph both listen on
    # `content_flushed`, so a create that only announced `document_created` left the
    # body out of the search index until a human happened to edit it. Agent-authored
    # documents are the ones nobody edits — 58 of 61 memory facts were unfindable.
    # DELIBERATE DOUBLE EMIT: the create_document
    # primitive now emits content_flushed itself, so this path emits twice. Keep
    # both — _finalize_content_mutation also rebuilds mentions/backlinks (which the
    # primitive does not), and the trailing debounce collapses the pair into one
    # embed (same reasoning jobs/tasks.py records for its own double emit).
    if content:
        await doc_state.finalize_content_mutation(doc_id, content, project_id)
    return {"doc_id": doc_id, "sort_key": payload.get("sort_key")}


async def create_reference_via_collab(
    *,
    document_id: str,
    title: str,
    content: str,
    media_type: str,
    source_url: str | None,
    project_id: str,
    user: dict,
    scope_root: str | None = None,
    author_name: str | None = None,
) -> dict:
    """Single source of truth for agent-driven reference creation.

    Mirrors `create_document_via_collab` for the reference variant: validates the
    title, then delegates the canonical row build + `reference_created` emit to
    `create_reference_row` (the shared creation chokepoint) with host_id = document_id.
    Both the Tool-API direct path (`_apply_create_reference_direct`) and the chat
    proposal apply path (`_apply_create_proposal` create_reference branch) delegate
    here so the two entry points can never drift on invariants.

    # ARCH (subtree-scoped agent keys): the host document must be inside the
    # subtree (a reference attaches to its host — the wall covers the anchor too).

    `author_name` (S1 attribution): the DISPLAY-axis byline — the making agent
    key's label, resolved by api_key_auth.agent_author_name at the surface. None
    (default / chat-agent callers) keeps the owning user's name.

    Returns {"doc_id": ...} (the new reference's id). Raises HTTPException on
    validation failure. The caller owns the access check and proposal bookkeeping.
    """
    from documents.service import create_reference_row
    from fastapi import HTTPException

    from scope import require_doc_in_scope

    title = (title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    # WHY (reference-host): a reference attaches to a real host. The tool
    # layer guarantees this (create_document 422s on a missing host; ToolImportFile's
    # model_validator 422s on is_reference without document_id); this is the
    # defense-in-depth gate for a path that reaches the primitive directly (e.g. a
    # malformed create_reference proposal). A clear 400 beats letting an empty
    # parent_id trip documents_reference_parent_check as an opaque 500. Then a
    # scoped key may only target an in-scope host.
    # INVARIANT (no host normalization): host_id = document_id verbatim — the service
    # takes it as-is and MUST NOT re-resolve to the project index doc. Why: this path
    # requires a real host (above); normalizing would let a malformed empty-host
    # proposal silently re-host on the index doc, a behavior change.
    if not document_id:
        raise HTTPException(status_code=400, detail="A reference requires a host document")
    await require_doc_in_scope(scope_root, document_id)
    ref_id = str(uuid4())
    try:
        await create_reference_row(
            ref_id=ref_id, project_id=project_id, host_id=document_id,
            title=title, content=content, media_type=media_type,
            source_url=source_url,
            created_by=user.get("user_id"),
            created_by_name=(
                author_name if author_name is not None else user.get("name")
            ),
        )
    except Exception:
        logger.exception("Agent create_reference failed (project=%s)", project_id)
        raise HTTPException(status_code=500, detail="Failed to create reference")
    return {"doc_id": ref_id}

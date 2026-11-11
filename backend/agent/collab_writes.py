"""Agent-driven create + checkpoint + presence helpers (shared by Tool-API + proposals).

Part of the chat-agent-mode system: the collab-backed create paths
(create_document_via_collab / create_reference_via_collab) and the shared
pre-edit checkpoint + presence broadcast. Internal extraction — no SYSTEM marker;
this module OWNS its names.
"""
import json
import logging
from uuid import uuid4

import artifacts
from fastapi import HTTPException
from markdown_normalize import normalize_list_spacing, unwrap_placeholder_brackets

import event_bus
from agent import doc_state
from table_serialize import extract_table_labels, import_gfm_tables
from ydoc_store import set_content

logger = logging.getLogger(__name__)


def convert_import_tables(
    content: str, check_cell_cap: bool,
) -> tuple[str | None, str | None, list[dict]]:
    """GFM tables → objects for a markdown body: the ONE conversion block the
    create factories and the empty-doc append share (the edit apply core stages
    the same pieces around its post-splice skip rule —
    apply_edits_resolver._convert_resolved_edit_tables).
    Returns ``(converted, tables_json, tables_out)`` — converted/tables_json
    are None and tables_out empty when nothing converted; a create caller
    births the row with the PRE-conversion ``content`` and, when tables_json
    is not None, replaces it via ``set_content(converted, persist=True,
    tables_json=…)`` in ONE Y.Doc update; an edit caller rewrites its splice's
    new_text to ``converted`` and hands the parsed models to
    ``route_document_edits(new_tables=…)``.

    ``check_cell_cap`` bounds each cell at AGENT_TABLE_MAX_CELL_CHARS (400,
    nothing written) — agent-AUTHORED bodies only; file imports are bounded by
    their file-size cap upstream, so the same ``.md`` behaves identically on
    every import route.

    # Public (no underscore): consumed across the chat/ package boundary by
    # routes/tool_api/edits.py (the empty-doc append) — the same sanctioned
    # seam as doc_state.reject_remote_images; test_module_boundaries forbids a
    # private reach from there.
    """
    converted, tables_json = import_gfm_tables(content)
    if tables_json == "{}":
        return None, None, []
    if check_cell_cap:
        from agent.table_writes import reject_table_overflow

        reject_table_overflow(
            [m["rows"] for m in json.loads(tables_json).values()]
        )
    tables_out = [
        {"table_id": tid, "label": label}
        for tid, label in extract_table_labels(converted).items()
    ]
    return converted, tables_json, tables_out


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
    media_type: str = "markdown",
    import_tables: bool = False,
    check_cell_cap: bool = False,
) -> dict:
    """Single source of truth for agent-driven document creation (PR4 R3).

    `is_system` marks the row as reserved system infrastructure (grouped in the
    tree apart from user documents).

    `media_type="html"`: the row is an interactive page
    leaf — the markdown passes (remote-image reject, list-spacing normalize,
    placeholder unwrap) are SKIPPED (the body is page source, not markdown),
    the artifact size cap applies, and the mention/embed finalize tail is
    skipped (a page cannot navigate; embeddings skip html rows).

    `import_tables`: when set and the body is markdown, every GFM table in
    `content` becomes a table object — file imports (zip, uploads, the
    converter worker, import_file) AND the agent-authored Tool-API creates set
    it (an authored pipe table is born as an object, never left as text). The row
    is born with the PRE-conversion body (the GFM read-model of its own future
    state — documents.content never holds anchor-form text, not even for a
    moment), then `set_content(converted, persist=True, tables_json=…)`
    replaces it in ONE Y.Doc update, and the finalize tail runs on the
    PRE-conversion body (= expand_tables — a cell's `[t](<id>)` link stays
    mention-visible; the anchor-form text is never what mentions read).
    `check_cell_cap` additionally bounds each cell at
    AGENT_TABLE_MAX_CELL_CHARS (400, nothing written) — agent-AUTHORED bodies
    only; file imports are bounded by their file-size cap upstream. Default
    False keeps memory cards, the zip's own row creation (content="") and html
    pages untouched.

    Validates title + content cap, normalizes list spacing, validates the parent,
    derives a unique path (preserving the sort_key invariant), creates the row,
    and emits `document_created` with sort_key so the client tree inserts
    correctly. The Tool-API create paths (direct apply and the confirm-mode
    retry — a confirm call is refused with 409 `confirmation_required` at the
    boundary and the driver retries the approved call through the same direct
    path) both land here, so the two entry points can never drift on
    invariants.

    # ARCH: when `scope_root` is set, a missing
    # parent_id defaults to the subtree root (the new doc lands INSIDE the wall),
    # and any explicit parent must already be in the subtree (else 403). Unscoped
    # keys keep today's behavior (parent_id as-passed).

    Returns {"doc_id": ..., "sort_key": ..., "tables": [{table_id, label}]?}.
    Raises HTTPException on validation failure. Caller is responsible for the
    access check (the two callers use different access-resolution helpers) and
    any proposal bookkeeping.
    """
    from documents.service import (
        assert_parent_valid,
        create_with_unique_path,
        effective_parent_id,
    )

    from config import MAX_PROPOSAL_NEW_TEXT_CHARS
    from scope import require_doc_in_scope, resolve_scoped_parent

    is_artifact = media_type == artifacts.ARTIFACT_MEDIA_TYPE
    title = (title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    if is_artifact:
        await artifacts.assert_artifact_size(len(content.encode("utf-8")))
    else:
        if len(content) > MAX_PROPOSAL_NEW_TEXT_CHARS:
            raise HTTPException(status_code=413, detail="Content too large")
        # Reject remote image URLs in authored text (degradation — no server-side
        # fetch; name the fix). Shared with the edit/append convergence path.
        doc_state.reject_remote_images(content)
        # Unwrap `(<id>)` placeholder-bracket destinations — one of the three agent
        # write sites for the unwrap (create).
        content = unwrap_placeholder_brackets(normalize_list_spacing(content))
    # GFM tables → objects: the conversion runs AFTER the checks above (they saw
    # the pre-conversion body) and BEFORE the row birth, so a cap refusal (400)
    # writes nothing. `content` stays PRE-conversion — the row is born with it.
    converted, tables_json, tables_out = (
        convert_import_tables(content, check_cell_cap)
        if import_tables and not is_artifact else (None, None, [])
    )
    # T5: under a scoped key, a missing parent defaults to the subtree root and an
    # explicit parent must lie inside the subtree (the wall cannot be bypassed by
    # choosing an out-of-scope parent).
    parent_id = resolve_scoped_parent(scope_root, parent_id)
    if parent_id:
        await require_doc_in_scope(scope_root, parent_id)
    await assert_parent_valid(parent_id, project_id)
    # "Under the index" IS the project root for a document (the tree renders
    # the index row childless). Stored-value normalisation AFTER the wall +
    # parent checks above — they ran on the id the caller sent.
    stored_parent = await effective_parent_id(parent_id, project_id)

    doc_id = str(uuid4())
    payload = {
        "project_id": project_id, "parent_id": stored_parent,
        "title": title, "content": content, "is_reference": False,
        "is_system": is_system,
    }
    if is_artifact:
        payload["media_type"] = artifacts.ARTIFACT_MEDIA_TYPE
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
        parent_id=stored_parent, is_reference=False,
        sort_key=payload.get("sort_key"),
        # WHY: the tree row a client appends from this frame is what Editor reads
        # media_type from; without it a fresh page opens as text until a reload.
        media_type=payload.get("media_type"),
    )
    if converted is not None:
        # ONE Y.Doc update replaces the born body: anchors + models land in a
        # single broadcast frame (the orphan INVARIANT on table_serialize).
        await set_content(doc_id, converted, persist=True,
                          tables_json=tables_json)    # WHY: a create that CARRIES a body runs the same finalize tail as an edit —
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
    # html artifacts skip the tail: mentions are gated off at the rebuild
    # chokepoint anyway and embeddings skip html rows — no read model to update.
    # WHY the tail takes the PRE-conversion body: after an import_tables
    # conversion it equals expand_tables(doc) — the GFM read-model — so a
    # mention link inside a converted cell ([t](<id>)) stays visible to
    # rebuild_doc_mentions. The anchor-form `converted` text is NEVER passed
    # here (cell links would be invisible = data loss).
    if content and not is_artifact:
        await doc_state.finalize_content_mutation(doc_id, content, project_id)
    result = {"doc_id": doc_id, "sort_key": payload.get("sort_key")}
    if tables_out:
        result["tables"] = tables_out
    return result


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
    import_tables: bool = False,
    check_cell_cap: bool = False,
) -> dict:
    """Single source of truth for agent-driven reference creation.

    Mirrors `create_document_via_collab` for the reference variant: validates the
    title, then delegates the canonical row build + `reference_created` emit to
    `create_reference_row` (the shared creation chokepoint) with host_id = document_id.
    The Tool-API reference paths (direct apply and the confirm-mode retry — see
    the 409 note on `create_document_via_collab`) both land here so the two
    entry points can never drift on invariants.

    `import_tables`: when set and media_type is markdown, every GFM table in
    `content` becomes a table object — file imports and the agent-authored
    Tool-API reference creates both set it. The row is born with the
    PRE-conversion body (documents.content is never anchor-form, not even for
    a moment), then `set_content(converted, persist=True, tables_json=…)`
    replaces it in ONE Y.Doc update, then the finalize tail runs on the
    PRE-conversion body (= expand_tables — a cell's `[t](<id>)` link stays
    mention-visible). This factory gains exactly ONE new check with it:
    `check_cell_cap` bounds each cell at AGENT_TABLE_MAX_CELL_CHARS (400,
    nothing written) for agent-AUTHORED bodies; no size cap, no remote-image
    guard here.

    # ARCH (subtree-scoped agent keys): the host document must be inside the
    # subtree (a reference attaches to its host — the wall covers the anchor too).

    `author_name` (S1 attribution): the DISPLAY-axis byline — the making agent
    key's label, resolved by api_key_auth.agent_author_name at the surface. None
    (default / chat-agent callers) keeps the owning user's name.

    Returns {"doc_id": ..., "tables": [{table_id, label}]?}. Raises HTTPException
    on validation failure. The caller owns the access check and proposal
    bookkeeping.
    """
    from documents.service import create_reference_row

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
    # GFM tables → objects, BEFORE the row birth (a cap refusal writes nothing).
    # `content` stays PRE-conversion — the row is born with it.
    is_markdown_body = import_tables and media_type == "markdown"
    converted, tables_json, tables_out = (
        convert_import_tables(content, check_cell_cap)
        if is_markdown_body else (None, None, [])
    )
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
    if converted is not None:
        # ONE Y.Doc update replaces the born body: anchors + models land in a
        # single broadcast frame (the orphan INVARIANT on table_serialize).
        await set_content(ref_id, converted, persist=True,
                          tables_json=tables_json)
    if is_markdown_body:
        # Finalize tail on the EXPANDED body (the PRE-conversion content equals
        # expand_tables(doc)): mention links inside converted cells stay visible
        # to rebuild_doc_mentions — the anchor-form text is never what mentions
        # read. The birth emit from create_reference_row + this content_flushed
        # collapse in the embedding debounce (one embed).
        await doc_state.finalize_content_mutation(ref_id, content, project_id)
    result = {"doc_id": ref_id}
    if tables_out:
        result["tables"] = tables_out
    return result

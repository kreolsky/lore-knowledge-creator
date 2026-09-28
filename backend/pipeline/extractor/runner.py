"""Agent Extractor runner — async task that runs the PocketFlow pipeline.

Handles:
- Event hook subscription for transcription_complete
- Manual trigger via API
- Creating child document and emitting project WS events
"""
# ARCH: Pipeline runs as arq job (extract_task) enqueued from the web process.
# ARCH: Emits document_created event so project WS broadcasts to all connected clients.
import logging
import uuid
from typing import TYPE_CHECKING

from documents.service import create_document

from db import extract_id, fetch_one, get_db
from event_bus import emit
from jobs import pool as jobs_pool
from models import is_ref_row
from pipeline.extractor.flow import create_extractor_flow
from pipeline.extractor.utils import render_title_template

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pipeline.extractor.params import ExtractorParams

# WHY: sentinel author for pipeline-generated messages. Backend serializer
# Why: coupling pipeline output to a real users-table row would break when accounts
# are deleted and could collide with a human author; the serializer maps this id to
# display name "system notes" without a users-table query.
PIPELINE_AUTHOR_ID = "__pipeline"


async def _fetch_user_timezone(user_id: str | None) -> str | None:
    # WHY: tz lookup must never fail the pipeline — UTC fallback is a fine degradation
    # (timestamp shifts a few hours in the doc title) compared to losing extracted data.
    if not user_id or user_id.startswith("__"):
        return None
    try:
        user = await fetch_one("users", user_id)
    except Exception as e:
        logger.warning("[Extractor] failed to load user %s for tz: %s", user_id, e)
        return None
    if not user:
        return None
    tz = user.get("timezone")
    return tz if isinstance(tz, str) and tz else None


def _extractor_shared(
    reference_id: str, source_doc_id: str, config_doc_id: str,
    target_doc_id: str, project_id: str, model: str | None,
) -> dict:
    """Build the flow's `shared` dict from resolved inputs — the ONE definition so
    the wet path (run_extractor) and the dry path (run_extractor_dry) feed the
    flow identical keys and cannot silently drift on a future resolved input."""
    return {
        "reference_id": reference_id,
        "source_doc_id": source_doc_id,
        "config_doc_id": config_doc_id,
        "target_doc_id": target_doc_id,
        "project_id": project_id,
        "model": model,
    }


async def run_extractor(
    reference_id: str,
    source_doc_id: str,
    config_doc_id: str,
    target_doc_id: str,
    project_id: str,
    title_template: str | None = None,
    model: str | None = None,
    user_id: str | None = None,
) -> None:
    """Run the full extractor pipeline for a single reference.

    Creates a child document under target_doc_id with rendered markdown.
    Emits document_created event for project WS broadcast.
    """
    logger.info(
        "[Extractor] Starting pipeline: ref=%s source_doc=%s config_doc=%s target_doc=%s project=%s model=%s",
        reference_id, source_doc_id, config_doc_id, target_doc_id, project_id, model or "(default)",
    )

    shared = _extractor_shared(
        reference_id, source_doc_id, config_doc_id, target_doc_id, project_id, model,
    )

    flow = create_extractor_flow()
    await flow.run_async(shared)

    # A duplicated variable key is silently last-wins in YAML (an earlier definition,
    # possibly a calculation, is lost). Non-blocking: extraction ran on the parsed dict,
    # but the silent loss must become visible. See SYSTEM: extractor.
    duplicates = shared.get("variable_duplicates") or []
    if duplicates:
        await _create_warning_note(
            project_id, source_doc_id, duplicates, config_doc_id=config_doc_id
        )

    rendered = shared.get("rendered_markdown", "")
    if not rendered:
        logger.warning("Extractor produced no output for ref %s", reference_id)
        return

    logger.info(
        "[Extractor] Pipeline completed for ref=%s. Rendered length=%d first300=%s",
        reference_id, len(rendered), rendered[:300],
    )

    doc_uid = str(uuid.uuid4())
    source_title = shared.get("source_doc_title", "Extraction")
    reference_title = shared.get("reference_title", "")
    tz_name = await _fetch_user_timezone(user_id)
    title = render_title_template(
        title_template, source_title,
        reference_title=reference_title, tz_name=tz_name,
    )

    # see SYSTEM: markdown content normalize — extractor entry point. Collapse irregular
    # post-marker spacing (e.g. `*   [ ]`) in the rendered template+LLM-values so the
    # STORED extracted doc is clean. Spacing-only (no reflow/dedent): extracted content
    # must not be reformatted wholesale, same rationale as the agent apply path.
    from markdown_normalize import normalize_list_spacing
    rendered = normalize_list_spacing(rendered)

    # create_document (not create_record) so the extracted doc gets a sort_key and
    # is reorderable — a raw create_record left sort_key NONE and broke drag-reorder.
    record = await create_document(doc_uid, {
        "project_id": project_id,
        "parent_id": target_doc_id,
        "title": title,
        "content": rendered,
        "path": f"{doc_uid}.md",
        "is_index": False,
    })

    # INVARIANT: document_created MUST carry sort_key. Why: without it the client adds
    # the new doc to its store keyless, so an immediate drag-reorder is reverted on the
    # next render and only "sticks" after a full reload re-fetches the real key
    # Mirrors the REST create endpoint's emit.
    await emit(
        "document_created",
        project_id=extract_id(project_id),
        document_id=doc_uid,
        title=title,
        parent_id=target_doc_id,
        sort_key=record.get("sort_key"),
    )

    logger.info("Extractor created document %s for ref %s", doc_uid, reference_id)


async def run_extractor_dry(params: "ExtractorParams") -> dict:
    """Run the extractor flow WITHOUT creating a document or emitting events.

    Same flow + resolution as `run_extractor`, minus the write side effects:
    populates `shared["extracted_data"]` (the post-compute final dict — what the
    benchmark scores against gold) and returns it + the rendered markdown + any
    duplicate-variable warnings. A dry/benchmark run therefore provably matches a
    real run's inputs (determinism is guaranteed at utils.py temperature=0) while
    never polluting the project.

    # INVARIANT: this path MUST NOT call create_document / emit. A benchmark dry-run
    # Why: a stray extracted doc would corrupt the score baseline; asserted in
    # test_dry_run_returns_data_without_side_effects (spies on both).
    """
    shared = _extractor_shared(
        params.reference_id, params.source_doc_id, params.config_doc_id,
        params.target_doc_id, params.project_id, params.model,
    )

    flow = create_extractor_flow()
    await flow.run_async(shared)

    return {
        "extracted_data": shared.get("extracted_data", {}),
        "rendered_markdown": shared.get("rendered_markdown", ""),
        "variable_duplicates": shared.get("variable_duplicates") or [],
    }


async def on_transcription_complete(reference_id: str, project_id: str, user_id: str | None = None, **kwargs) -> None:
    """Event hook: check if source document has agent config, enqueue extract_task if so.

    # WHY (drift): this is the AUTO-trigger resolution path. The MANUAL path
    # (editor `run_agent` + MCP `run_extractor`) lives in resolve_extractor_params
    # (pipeline/extractor/params.py), which is the single source for those callers.
    # They are intentionally NOT unified: the event hook has no user/session context
    # (it cannot run require_project_full), filters to trigger_event=
    # 'transcription_complete', and resolves silently (returns, never raises). The
    # agent_configs SELECT + per-row {config_doc_id, target_doc_id, title_template,
    # model} mapping here MUST stay aligned with resolve_extractor_params — if you
    # add an agent_configs field or change parent_id normalization, update BOTH
    # sites. Why: the two paths feed the same extract_task; a silent divergence here
    # is exactly the parity class this alignment rule exists to prevent.
    """
    # Resolved as soon as the reference's host is known — the failure branch below
    # needs it to leave the error note on the SOURCE document even when the failure
    # is the agent_configs lookup or the enqueue itself.
    doc_id: str | None = None
    try:
        ref = await fetch_one("documents", reference_id)
        if not ref or not is_ref_row(ref):
            return

        from db import extract_id
        doc_id = extract_id(ref.get("parent_id"))
        if not doc_id:
            return

        db = await get_db()
        rows = await db.query(
            "SELECT * FROM agent_configs WHERE document_id = $did AND trigger_event = $evt AND deleted_at IS NONE",
            {"did": doc_id, "evt": "transcription_complete"},
        )
        if not rows:
            return

        for config in rows:
            config_doc_id = extract_id(config.get("config_doc_id"))
            target_doc_id = extract_id(config.get("target_doc_id"))
            title_template = config.get("title_template")
            model = config.get("model")

            if not config_doc_id or not target_doc_id:
                logger.warning("Agent config incomplete for doc %s", doc_id)
                continue

            job_id = f"extract:{reference_id}:{config_doc_id}:{target_doc_id}"
            await jobs_pool.enqueue(
                "extract_task",
                reference_id, doc_id, config_doc_id, target_doc_id, extract_id(project_id),
                title_template=title_template, model=model, user_id=user_id,
                job_id=job_id,
            )
            await emit("agent_extraction_started",
                       reference_id=reference_id,
                       project_id=extract_id(project_id))
            logger.info("Auto-triggered extractor for ref %s", reference_id)

    except Exception as e:
        logger.error("Extractor hook error for ref %s: %s", reference_id, e)
        # No silent degradation: the user CONFIGURED auto-extraction for this
        # document, and it just silently never ran. Leave the standard pipeline
        # error note on the source document (best-effort, never raises) — the log
        # line alone is invisible to the user. Without the host id (the failure
        # was the reference fetch itself) there is nothing to anchor a note on,
        # and the log is all this hook can do.
        if doc_id:
            await _create_error_note(project_id, doc_id, reference_id, e)


async def _resolve_doc_title(doc_id: str | None) -> str:
    """Best-effort title enrichment: a doc's human title, or the raw id.

    WHY: notes name their config/reference by title when one exists; the id is
    the fallback so a vanished/unreadable doc degrades to a still-resolvable name.
    """
    if not doc_id:
        return "unknown"
    try:
        doc = await fetch_one("documents", doc_id)
        if doc:
            return doc.get("title") or doc_id
    except Exception:
        pass  # WHY: best-effort title enrichment; failure falls back to the raw id.
    return doc_id


async def _emit_note_created(document_id: str, session_uid: str) -> None:
    """Best-effort realtime nudge so a system note appears live in open NotesPanels.

    # ARCH: minimal payload (session_id only) — the receiving replica does a
    # best-effort loadSessions rather than upserting an empty card. Worker →
    # backplane → web replica path, same as checkpoint_created. Fire-and-forget:
    # a publish failure must not break note creation (mirrors the caller's
    # best-effort contract).
    # The extractor's notes are pinned to the SOURCE document (a real, openable
    # doc) — entity_id = document_id directly; reference resolution lives in the
    # chat routes, for user-created notes only. No chat-private import (module-boundary test).
    """
    try:
        await emit("note_session_created",
                   entity_type="doc",
                   entity_id=document_id,
                   event={"type": "note_session_created", "session_id": session_uid})
    except Exception:
        logger.warning("note_session_created emit failed (extractor)", exc_info=True)


async def _make_pipeline_note(
    *,
    project_id: str,
    document_id: str,
    title: str,
    body: str,
) -> str | None:
    """ONE home for the pipeline's system notes (warning + error).

    Creates an is_note chat_session via the shared system-note primitive and
    nudges open NotesPanels. Best-effort by contract: returns the session uid
    or None, and NEVER raises — a diagnostic note must not break the flow that
    spawned it (the successful extraction, or the error path re-raising the
    original error).

    # ARCH: delegates persistence to the shared system-note primitive
    # (notes_service) so the is_note chat_session + message shape is owned in
    # ONE place, shared with the agent error-note path. Returns None on
    # failure (never raises).
    """
    try:
        from notes_service import create_system_note

        session_uid = await create_system_note(
            project_id=project_id,
            document_id=document_id,
            title=title,
            body=body,
            author_id=PIPELINE_AUTHOR_ID,
        )
        if session_uid is not None:
            await _emit_note_created(document_id, session_uid)
        return session_uid
    except Exception as note_err:
        logger.error("Failed to create %s note on doc %s: %s", title, document_id, note_err)
        return None


async def _create_warning_note(
    project_id: str,
    document_id: str,
    duplicates: list[str],
    config_doc_id: str | None = None,
) -> None:
    """Create a non-blocking warning note listing duplicated variable definitions.

    Sibling of _create_error_note — same system-note primitive, "Warning" title.
    Best-effort: a failure here must never break the (successful) extraction.
    """
    config_title = await _resolve_doc_title(config_doc_id)

    dup_list = ", ".join(f"`{d}`" for d in duplicates)
    config_line = (
        f"**Config:** [{config_title}](doc:{config_doc_id})\n\n" if config_doc_id else ""
    )
    body = (
        f"## Pipeline Warning: duplicate variables\n\n"
        f"{config_line}"
        f"These variable keys are defined more than once in the variables document. "
        f"YAML keeps only the last definition — earlier ones (including any "
        f"calculations) are silently dropped. Remove the duplicates to be safe.\n\n"
        f"Duplicated: {dup_list}"
    )

    session_uid = await _make_pipeline_note(
        project_id=project_id,
        document_id=document_id,
        title="Pipeline Warning",
        body=body,
    )
    if session_uid is not None:
        logger.info(
            "Created duplicate-variable warning note %s on doc %s (%d dups)",
            session_uid, document_id, len(duplicates),
        )


async def _create_error_note(
    project_id: str,
    document_id: str,
    reference_id: str,
    error: Exception,
    config_doc_id: str | None = None,
) -> None:
    """Create an anchorless note on the source document describing the pipeline error."""
    ref_title = await _resolve_doc_title(reference_id)
    config_title = await _resolve_doc_title(config_doc_id)

    ref_link = f"[{ref_title}](ref:{reference_id})"
    config_line = f"**Config:** [{config_title}](doc:{config_doc_id})" if config_doc_id else ""

    error_text = (
        f"## Pipeline Error\n\n"
        f"**Reference:** {ref_link}"
    )
    if config_line:
        error_text += f"\n{config_line}"
    error_text += f"\n\n```\n{error}\n```"

    session_uid = await _make_pipeline_note(
        project_id=project_id,
        document_id=document_id,
        title="Pipeline Error",
        body=error_text,
    )

    pid = extract_id(project_id)
    if session_uid is not None:
        try:
            await emit("extraction_error",
                        project_id=pid,
                        reference_id=reference_id,
                        note_id=session_uid,
                        document_id=document_id)
            logger.info("Created error note %s on doc %s for ref %s", session_uid, document_id, reference_id)
        except Exception as note_err:
            logger.error("Failed to create error note for ref %s: %s", reference_id, note_err)


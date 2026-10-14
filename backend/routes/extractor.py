"""Agent config CRUD + manual extractor / digest trigger routes.

CRUD for agent_configs table. Multiple configs per document allowed
(distinguished by trigger_event and kind). Manual trigger enqueues extract_task
per reference, or digest_task per digest binding.
"""
# ARCH: Multiple agent configs per source document (index on document_id + trigger_event).
# ARCH: Pipeline runs as arq job — 200 accepted returned immediately.
# SYSTEM: extractor-routes — CRUD + manual trigger for agent extractor pipeline
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pipeline.digest.triggers import enqueue_digest
from pipeline.extractor.params import EXTRACTOR_ROWS, resolve_extractor_params
from surrealdb import AsyncSurreal

from access import require_digest_editor, require_project_full, resolve_digest_subjects
from auth import get_current_user
from db import (
    create_record,
    extract_id,
    fetch_one,
    get_db,
    serialize_record,
    soft_delete,
)
from jobs import pool as jobs_pool
from models import AgentConfigCreate, AgentRunRequest

router = APIRouter(prefix="/api")


@router.get("/agent-configs")
async def get_agent_configs(
    document_id: str,
    user=Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    rows = await db.query(
        "SELECT * FROM agent_configs WHERE document_id = $did AND deleted_at IS NONE",
        {"did": document_id},
    )
    if not rows:
        return []
    project_id = rows[0].get("project_id", "")
    await require_project_full(project_id, user)
    return [serialize_record(r, "config_id") for r in rows]


@router.get("/agent-config")
async def get_agent_config(
    document_id: str,
    trigger_event: str = "transcription_complete",
    user=Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    # WHY: this read gates the per-reference "send to agent" (extractor) button, so a
    # digest binding alone must read as "no extractor configured".
    rows = await db.query(
        f"SELECT * FROM agent_configs WHERE document_id = $did AND trigger_event = $evt AND {EXTRACTOR_ROWS} "
        "AND deleted_at IS NONE LIMIT 1",
        {"did": document_id, "evt": trigger_event},
    )
    if not rows:
        return None
    config = rows[0]
    project_id = config.get("project_id", "")
    await require_project_full(project_id, user)
    return serialize_record(config, "config_id")


@router.put("/agent-config")
async def upsert_agent_config(
    body: AgentConfigCreate,
    user=Depends(get_current_user),
    db: AsyncSurreal = Depends(get_db),
):
    config_doc = await fetch_one("documents", body.config_doc_id)
    if not config_doc:
        raise HTTPException(status_code=400, detail="Config document not found")
    target_doc = await fetch_one("documents", body.target_doc_id)
    if not target_doc:
        raise HTTPException(status_code=400, detail="Target document not found")
    source_doc = await fetch_one("documents", body.document_id)
    if not source_doc:
        raise HTTPException(status_code=400, detail="Source document not found")

    project_id = source_doc.get("project_id", "")
    if config_doc.get("project_id") != project_id or target_doc.get("project_id") != project_id:
        raise HTTPException(status_code=400, detail="Config and target documents must be in the same project as source")

    await require_project_full(project_id, user)
    if body.kind != "digest" and body.subject_user_ids is not None:
        raise HTTPException(status_code=400, detail="subject_user_ids applies only to a digest binding")

    # WHY: `trigger_event IS NONE` matches legacy rows created before the field
    # was added — without this, upsert wouldn't find them and would create duplicates.
    # `kind IS NONE` is the same for rows predating `kind`: they are extractor rows.
    # Kind is part of the key, so a digest and an extractor coexist on one document.
    binding_key = (
        "document_id = $did "
        "AND (trigger_event = $evt OR (trigger_event IS NONE AND $evt = 'transcription_complete')) "
        "AND (kind = $kind OR (kind IS NONE AND $kind = 'extractor'))"
    )
    key_vars = {"did": body.document_id, "evt": body.trigger_event, "kind": body.kind}
    live = await db.query(
        f"SELECT * FROM agent_configs WHERE {binding_key} AND deleted_at IS NONE LIMIT 1", key_vars,
    )
    revived = [] if live else await db.query(
        f"SELECT * FROM agent_configs WHERE {binding_key} AND deleted_at IS NOT NONE LIMIT 1", key_vars,
    )

    # created_by is stamped once and never changed by an update; a revived
    # soft-deleted row is a new binding, so its caller becomes the creator.
    previous = live[0] if live else {}
    created_by = previous.get("created_by") or user["user_id"]
    subjects = None
    if body.kind == "digest":
        if live:
            await require_digest_editor(project_id, user, previous.get("created_by"))
        requested = body.subject_user_ids if body.subject_user_ids is not None else previous.get("subject_user_ids")
        subjects = await resolve_digest_subjects(project_id, user, created_by, requested)

    fields = {
        "config_doc_id": body.config_doc_id, "target_doc_id": body.target_doc_id,
        "project_id": project_id, "title_template": body.title_template, "model": body.model,
        "trigger_event": body.trigger_event, "kind": body.kind,
        "created_by": created_by, "subject_user_ids": subjects,
    }
    existing = live or revived
    if existing:
        config_id = extract_id(existing[0].get("id"))
        assignments = ", ".join(f"{name} = ${name}" for name in fields)
        await db.query(
            f"UPDATE type::record('agent_configs', $id) SET {assignments}, deleted_at = NONE",
            {"id": config_id, **fields},
        )
        updated = await fetch_one("agent_configs", config_id)
        return serialize_record(updated, "config_id")

    record = await create_record("agent_configs", str(uuid4()), {"document_id": body.document_id, **fields})
    return serialize_record(record, "config_id")


@router.delete("/agent-config/{config_id}")
async def delete_agent_config(
    config_id: str,
    user=Depends(get_current_user),
):
    config = await fetch_one("agent_configs", config_id)
    if not config:
        raise HTTPException(status_code=404, detail="Agent config not found")
    await require_project_full(config.get("project_id", ""), user)
    await soft_delete("agent_configs", config_id)
    return {"ok": True}


@router.post("/agent-config/run")
async def run_agent(
    body: AgentRunRequest,
    user=Depends(get_current_user),
):
    if body.config_id:
        return await _run_digest_binding(body.config_id, user)
    # ARCH: param resolution is shared with the MCP `run_extractor` tool via
    # resolve_extractor_params — the editor and the benchmark resolve the SAME way
    # (reference → parent source → agent_configs), so the two paths cannot drift.
    params_list = await resolve_extractor_params(body.document_id, user)

    # INVARIANT(security): the pipeline writes its own output — this call only enqueues,
    # and the worker creates the document under the launch consent granted above.
    # Why: independence from the caller is what a pipeline IS; a caller does not need,
    # and is not given, a separate right to save the result. Both halves of the model
    # live in the module docstring of pipeline.extractor.params.
    for params in params_list:
        job_id = f"extract:{params.reference_id}:{params.config_doc_id}:{params.target_doc_id}"
        await jobs_pool.enqueue(
            "extract_task",
            params.reference_id, params.source_doc_id, params.config_doc_id,
            params.target_doc_id, params.project_id,
            title_template=params.title_template, model=params.model,
            user_id=user.get("user_id"),
            job_id=job_id,
        )

    return {"status": "accepted"}


async def _run_digest_binding(config_id: str, user: dict) -> dict:
    config = await fetch_one("agent_configs", config_id)
    if not config or config.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Agent config not found")
    project_id = extract_id(config.get("project_id", ""))
    # Launch consent, as for the extractor above: the digest then writes its own report.
    await require_project_full(project_id, user)
    if config.get("kind") != "digest":
        raise HTTPException(status_code=400, detail="An extractor runs per reference: pass its document_id")
    await require_digest_editor(project_id, user, config.get("created_by"))
    await enqueue_digest(config_id, project_id)
    return {"status": "accepted"}

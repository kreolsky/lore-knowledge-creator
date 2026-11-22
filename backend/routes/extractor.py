"""Agent config CRUD + manual extractor / digest trigger routes.

CRUD for agent_configs table. Multiple configs per document allowed
(distinguished by trigger_event and kind). Manual trigger enqueues extract_task
per reference, or digest_task per digest binding.

The mutating bodies live in pipeline/core/bindings.py (ONE body, two callers:
this editor surface and the agent's tool-api handlers) — these routes only
resolve dependencies and delegate.
"""
# ARCH: Multiple agent configs per source document (index on document_id + trigger_event).
# ARCH: Pipeline runs as arq job — 200 accepted returned immediately.
# SYSTEM: extractor-routes — CRUD + manual trigger for agent extractor pipeline
from fastapi import APIRouter, Depends
from pipeline.core import bindings
from pipeline.extractor.params import EXTRACTOR_ROWS
from surrealdb import AsyncSurreal

from access import require_project_full
from auth import get_current_user
from db import get_db, serialize_record
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
):
    return await bindings.upsert_agent_config(body, user)


@router.delete("/agent-config/{config_id}")
async def delete_agent_config(
    config_id: str,
    user=Depends(get_current_user),
):
    return await bindings.delete_agent_config(config_id, user)


@router.post("/agent-config/run")
async def run_agent(
    body: AgentRunRequest,
    user=Depends(get_current_user),
):
    return await bindings.run_agent(body, user)

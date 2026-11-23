"""Tool-API — document properties + the task loop: the `tasks` skill's pack
(find_documents over the YAML-header projection, next_task / claim_task /
set_task_status over
the convention; SYSTEM: doc-properties, SYSTEM: task-convention).

Thin by design: the skill-disabled gate, `where` shape validation (a 422 the
model's loop self-corrects from), the mutation gates (gate_mutation_target +
the apply resolution) and delegation to the ONE module that owns each half
(backend/doc_properties.py, backend/task_convention.py) — no business logic
here.
"""
from datetime import datetime, timezone

from agent.context import get_agent_context
from artifacts import is_artifact_row
from fastapi import Depends, HTTPException
from page_lock import locked_doc_ids

from db import get_db
from doc_properties import PROP_KEY_RE, split_results_errors, visible_header_docs
from models import ToolClaimTask, ToolFindDocuments, ToolNextTask, ToolSetTaskStatus
from routes.tool_api._common import (
    _is_system_target,
    _refuse_tasks_disabled,
    _refuse_unconfirmable,
    _resolve_apply_or_force,
)
from routes.tool_api_telemetry import track_agent_tool
from scope import gate_mutation_target
from task_convention import (
    AGENT_NAME_RE,
    check_target_status,
    execute_claim,
    execute_set_status,
    resolve_task_schema,
    select_next_task,
)

#: The scalar JSON shapes a `where` value may take (a list value is OR-of
#: these). Anything else is a 422, never a silent no-match.
_WHERE_SCALARS = (str, int, float, bool, type(None))


def _validate_where(where: dict) -> None:
    """422 on a `where` this grammar cannot honor — a bad key or a value that
    is neither a JSON scalar nor a non-empty list of them. Key shape is
    PROP_KEY_RE (the same token grammar every property consumer binds), so a
    hostile key never reaches a query string."""
    for key, value in where.items():
        if not PROP_KEY_RE.match(key):
            raise HTTPException(
                status_code=422,
                detail=f"where key {key!r} must match {PROP_KEY_RE.pattern}",
            )
        values = value if isinstance(value, list) else [value]
        if not values or not all(isinstance(v, _WHERE_SCALARS) for v in values):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"where[{key!r}] must be a string, number, boolean, null "
                    "or a non-empty list of them"
                ),
            )


@track_agent_tool("find_documents")
async def tool_find_documents(
    body: ToolFindDocuments, ctx: dict = Depends(get_agent_context),
):
    """Visible documents by header properties: exact top-level-key equality
    (typed — `1` is not `\"1\"`), `under` narrowing to a subtree, and the
    visible-with-broken-header list in `header_errors` so a typo'd task is
    reported, never silently absent."""
    await _refuse_tasks_disabled(await get_db())
    where = body.where or {}
    _validate_where(where)
    rows = await visible_header_docs(
        project_id=ctx["project_id"], user=ctx["user"],
        scope_root=ctx.get("scope_root"), under=body.under,
    )
    return split_results_errors(rows, where, body.limit)


@track_agent_tool("next_task")
async def tool_next_task(
    body: ToolNextTask, ctx: dict = Depends(get_agent_context),
):
    """The offer read: the first available task (ready or a stale claim,
    dependencies closed) over the same visibility as find_documents, with
    the resolved schema's `initial` so an agent authoring discovered work
    never hardcodes a status."""
    await _refuse_tasks_disabled(await get_db())
    # WHY: `under` narrows the CANDIDATES only. The schema and the dependency
    # targets resolve over everything the key sees — the schema usually sits
    # at the project root, and a task in a story may depend on one outside
    # it; claim_task resolves the schema over the same un-narrowed set.
    visible = await visible_header_docs(
        project_id=ctx["project_id"], user=ctx["user"],
        scope_root=ctx.get("scope_root"),
    )
    schema = resolve_task_schema(visible)
    candidates = visible if not body.under else await visible_header_docs(
        project_id=ctx["project_id"], user=ctx["user"],
        scope_root=ctx.get("scope_root"), under=body.under,
    )
    return select_next_task(
        candidates, schema, now=datetime.now(timezone.utc), lookup=visible,
        agent_name=(body.agent_name or "").strip() or None,
    )


async def _gate_task_write(body, ctx: dict, tool: str) -> tuple[str, dict]:
    """The shared gate of the two task writes: skill-disabled, agent_name
    grammar (422), mutation target, PIN lock, artifact. Returns
    (agent_name, target row)."""
    await _refuse_tasks_disabled(await get_db())
    agent_name = (body.agent_name or "").strip()
    if not AGENT_NAME_RE.match(agent_name):
        raise HTTPException(
            status_code=422,
            detail=f"agent_name must match {AGENT_NAME_RE.pattern}",
        )
    target, _ = await gate_mutation_target(
        user=ctx["user"], doc_id=body.document_id,
        project_id=ctx["project_id"], scope_root=ctx.get("scope_root"),
    )
    # INVARIANT(security): a page the key owner PIN-locked is refused before
    # its header is read — the refusal text must not say "not found".
    # Why: find_documents/next_task hide locked pages and read_document
    # refuses them; a claim's 409 would otherwise echo the locked header's
    # status and assignee.
    if body.document_id in await locked_doc_ids(ctx["user"]["user_id"]):
        raise HTTPException(status_code=403, detail="This page is locked by the user")
    if is_artifact_row(target):
        raise HTTPException(
            status_code=422,
            detail=f"{tool} is a markdown-header operation — an html "
                   "artifact is not a task document",
        )
    return agent_name, target


@track_agent_tool("claim_task")
async def tool_claim_task(
    body: ToolClaimTask, ctx: dict = Depends(get_agent_context),
):
    """The loop's one write: the header-only claim splice (status → the
    schema's claimed status, assignee + claimed_until upserted) under the
    cross-replica edit lock, decided on the live text — a contested claim
    answers 409 already_claimed, never a silent takeover."""
    agent_name, target = await _gate_task_write(body, ctx, "claim_task")
    decision = await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value,
        is_system=await _is_system_target(
            body.document_id, ctx["project_id"], target,
        ),
    )
    if decision.mode != "auto":
        _refuse_unconfirmable("claim_task", decision.reason)
    rows = await visible_header_docs(
        project_id=ctx["project_id"], user=ctx["user"],
        scope_root=ctx.get("scope_root"),
    )
    schema = resolve_task_schema(rows)
    return await execute_claim(
        document_id=body.document_id, agent_name=agent_name,
        project_id=ctx["project_id"], schema=schema,
    )


@track_agent_tool("set_task_status")
async def tool_set_task_status(
    body: ToolSetTaskStatus, ctx: dict = Depends(get_agent_context),
):
    """The status transition: hand back or close a task — a header splice
    (status replaced, claimed_until dropped) decided on the live text under
    the edit lock, plus the archive move for a closed top-level task."""
    agent_name, target = await _gate_task_write(body, ctx, "set_task_status")
    pid = ctx["project_id"]
    rows = await visible_header_docs(
        project_id=pid, user=ctx["user"], scope_root=ctx.get("scope_root"),
    )
    schema = resolve_task_schema(rows)
    check_target_status(body.status, schema)
    # WHY: a close that may archive is also a move INTO the archive — the
    # move tool's rule: the destination parent decides the system cell too.
    archive_id = schema["archive_id"] if body.status in schema["closed"] else None
    is_system = await _is_system_target(body.document_id, pid, target) or bool(
        archive_id and await _is_system_target(archive_id, pid)
    )
    decision = await _resolve_apply_or_force(
        ctx, ui_preference=body.apply.value, is_system=is_system,
    )
    if decision.mode != "auto":
        _refuse_unconfirmable("set_task_status", decision.reason)
    return await execute_set_status(
        document_id=body.document_id, status=body.status,
        agent_name=agent_name, project_id=pid, schema=schema, rows=rows,
        user=ctx["user"], scope_root=ctx.get("scope_root"),
    )

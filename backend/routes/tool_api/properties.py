"""Tool-API — document properties + the task loop: the `tasks` skill's pack
(find_documents over the YAML-header projection, next_task / claim_task /
set_task_status over
the convention; SYSTEM: doc-properties, SYSTEM: task-convention).

Thin by design: the skill-disabled gate, `where` shape validation (a 422 the
model's loop self-corrects from), the mutation gates (gate_mutation_target +
the apply resolution) and delegation to the ONE module that owns each half
(backend/doc_properties.py, backend/task_convention.py) — no business logic
here but the bell: the note a question travels in becomes a system note on
the task through the widget note's own sequence (SYSTEM: notes, SYSTEM: inbox).
"""
import logging
from datetime import datetime, timezone

from agent.context import get_agent_context
from artifacts import is_artifact_row
from fastapi import Depends, HTTPException
from inbox import KIND_NOTE, mark_arrived
from notes_service import create_system_note
from page_lock import locked_doc_ids
from pipeline.extractor.runner import PIPELINE_AUTHOR_ID

from db import get_db
from doc_properties import PROP_KEY_RE, split_results_errors, visible_header_docs
from event_bus import emit
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

logger = logging.getLogger(__name__)

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
    """The status transition: hand back, close or park a task on a question —
    a header splice (status replaced, claimed_until dropped, assignee dropped
    on a return) decided on the live text under the edit lock, plus the
    archive move for a closed top-level task. A `note` rings the bell AFTER
    the write: the answer says `created` or `failed` beside the applied
    status."""
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
    result = await execute_set_status(
        document_id=body.document_id, status=body.status,
        agent_name=agent_name, project_id=pid, schema=schema, rows=rows,
        user=ctx["user"], scope_root=ctx.get("scope_root"),
        with_note=body.note is not None,
    )
    if body.note is not None:
        result["note"] = await _ring_bell(
            question=body.note, ctx=ctx, document_id=body.document_id,
            status=body.status, agent_name=agent_name, title=target.get("title"),
        )
    return result


async def _ring_bell(*, question: str, ctx: dict, document_id: str,
                     status: str, agent_name: str, title: str | None) -> str:
    """The bell: the question as a system note on the task, flagged for the
    key's owner unless the key is internal, announced live — the widget
    note's sequence (routes/widget.py). Runs after the status is applied.

    The server adds the link after the question (the note card previews the
    first message), so no agent can forget it; `[` and `]` leave the title
    and its whitespace is collapsed, because the note renders through
    MarkdownContent, whose link label allows no brackets.

    "created" promises the note, not the yellow row: an internal key and a
    notes toggle that is OFF leave it unmarked. A failure is logged and
    answered "failed" beside the applied status — never a 500 that hides it.
    """
    label = " ".join((title or "").replace("[", "").replace("]", "").split()) or document_id
    try:
        session_uid = await create_system_note(
            project_id=ctx["project_id"], document_id=document_id,
            title=f"{agent_name} — {status}",
            body=f"{question}\n\n[{label}](doc:{document_id})",
            author_id=PIPELINE_AUTHOR_ID,
        )
        if session_uid is None:
            return "failed"  # create_system_note logged its own error
        if ctx.get("internal") is not True:
            await mark_arrived(
                KIND_NOTE, object_id=session_uid, project_id=ctx["project_id"],
                document_id=document_id, recipient_id=ctx["user_id"],
            )
        await emit(
            "note_session_created", entity_type="doc", entity_id=document_id,
            event={"type": "note_session_created", "session_id": session_uid},
        )
    except Exception:  # noqa: BLE001
        logger.exception("set_task_status: the bell failed on doc %s", document_id)
        return "failed"
    return "created"

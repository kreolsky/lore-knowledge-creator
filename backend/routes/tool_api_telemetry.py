"""Agent-tool usage telemetry — the @track_agent_tool decorator.

# see SYSTEM: telemetry — agent_tool/call producer.
# ARCH: a thin decorator on each Tool-API handler (the agent's permission
#       boundary) that records ONE telemetry_event row per call: which tool ran,
#       the outcome (ok/error), the HTTP status, normalized validator feedback
#       (is the error self-correctable by the agent?), latency, the apply mode,
#       and correlation ids (X-Agent-Call-Id / X-Agent-Session-Id) so a row can
#       be joined to the driver log's tool-call ids for turn/retry analysis.
#
# INVARIANT: telemetry is fire-and-forget. A recording failure MUST NEVER break
# Why: telemetry sits on the hot tool path each agent turn depends on; its errors must never stall it.
# or delay a tool call (matches telemetry_store's append-only INVARIANT and the
# "no silent degradation" rule — but in the inverse direction: telemetry is
# strictly additive best-effort, so its own errors are swallowed + logged, never
# surfaced to the caller). Why: this decorator sits on the hot tool path the
# agent's turn depends on.
#
# Out of scope (by design): auth-layer errors (401/429 from get_agent_context)
# are NOT captured — the decorator wraps the handler, which runs AFTER successful
# auth, and an auth failure is not "tool usage".
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Any

from fastapi import HTTPException

from telemetry_store import clamp_detail, record_telemetry_events

logger = logging.getLogger(__name__)

# Per-event cap for the args blob (FULL body.model_dump). Reuses the shared
# telemetry_store.clamp_detail (identical to the collab/perf producers) so the cap +
# truncate sentinel never drift between producers writing the same detail column.
MAX_DETAIL_BYTES = 4096

# Validator feedback: the Tool-API IS the agent's permission/validator boundary.
# A 4xx in {404,409,422} is feedback the agent can self-correct from in the same
# turn (re-read, fix the edit range, supply the required parent_id). {403,429,5xx}
# are NOT self-correctable (rights/budget/server) and are reported separately.
_VALIDATOR_KINDS = frozenset({"not_found", "conflict", "validation"})


def error_kind_for(status: int | None) -> str | None:
    """Map an HTTP status to a normalized validator/transport error kind.

    Returns None for unmapped statuses (e.g. 400) and for None (the ok path).
    """
    if status is None:
        return None
    mapping = {
        404: "not_found",
        403: "access_denied",
        409: "conflict",
        422: "validation",
        429: "rate_limit",
    }
    if status in mapping:
        return mapping[status]
    if status >= 500:
        return "server_error"
    return None


def is_validator_feedback(error_kind: str | None) -> bool:
    """True for self-correctable validator feedback (404/409/422 kinds)."""
    return error_kind in _VALIDATOR_KINDS


def _apply_mode_of(body: Any) -> str | None:
    """Read body.apply.value when the tool carries an apply mode, else None.

    Read-only tools have no `apply` field; mutating tools carry an enum whose
    `.value` is 'auto' | 'confirm'. getattr guards both absence and non-enum bodies.
    """
    apply = getattr(body, "apply", None)
    return getattr(apply, "value", None)


def result_shape_of(result: Any) -> dict:
    """The window shape of a bounded read result: its total size and whether the
    answer was truncated.

    Derived from the handler's OWN return value (the decorator already reads
    `status` from it), so the pair joins the `offset`/`limit` the model asked for
    on the same row — "did the model page, and how far" becomes one query. A
    result with no window contributes nothing, so no other tool's row grows.
    `next_offset` is the read contract's only "there is more" signal, so its
    presence IS the truncation flag (see the read_document INVARIANT).
    """
    if not isinstance(result, dict) or "total_chars" not in result:
        return {}
    return {
        "total_chars": result.get("total_chars"),
        "truncated": "next_offset" in result,
    }


def build_detail(
    tool: str, body: Any, outcome: dict, latency_ms: int, ctx: dict,
) -> dict:
    """Build the `detail` object for one agent_tool/call telemetry row.

    `outcome` carries {ok, status, detail, result_status} captured by the decorator.
    `ctx` is the agent auth context (user_id/project_id/key_id + call_id/session_id).
    args is the FULL body.model_dump() clamped to MAX_DETAIL_BYTES.
    """
    args_raw: dict = {}
    if body is not None:
        try:
            args_raw = body.model_dump() or {}
        except Exception:
            args_raw = {}

    error_kind = error_kind_for(outcome["status"])
    # entity_id: derive a document id from the args when present (document_id for
    # most tools; parent_id is the host for reference creates). None for reads that
    # carry no single target (e.g. get_project_structure without start_id).
    entity_id = args_raw.get("document_id") or args_raw.get("parent_id")

    return {
        # A bounded read contributes total_chars + truncated here; every other
        # tool contributes nothing (see result_shape_of).
        **(outcome.get("result_shape") or {}),
        "tool": tool,
        "outcome": "ok" if outcome["ok"] else "error",
        "http_status": outcome["status"],
        "error_kind": error_kind,
        "is_validator_feedback": is_validator_feedback(error_kind),
        "result_status": outcome["result_status"],
        "latency_ms": latency_ms,
        "apply_mode": _apply_mode_of(body),
        "call_id": ctx.get("call_id"),
        "session_id": ctx.get("session_id"),
        # key_id: the acting agent key — the
        # system fact at apply time. The audit source-of-truth for granted auto-applies
        # (query telemetry_event by detail.key_id where result_status='applied'). The
        # field is on ctx for BOTH surfaces; it is purely additive here.
        "key_id": ctx.get("key_id"),
        "args": clamp_detail(args_raw),
        "error_detail": outcome["detail"],
        "entity_id": entity_id,
    }


# Fire-and-forget bookkeeping: hold strong refs to spawned tasks so the event loop
# doesn't garbage-collect a pending telemetry write mid-flight (a "task was
# destroyed but it is pending" warning + a silently-dropped row).
_pending_telemetry: set[asyncio.Task] = set()


async def _insert(detail: dict, ctx: dict) -> None:
    """Build the full telemetry row from detail + ctx and insert it (best-effort)."""
    row = {
        "category": "agent_tool",
        "kind": "call",
        "user_id": ctx.get("user_id") or "",
        "project_id": ctx.get("project_id"),
        "entity_id": detail.pop("entity_id", None),
        "detail": detail,
        "client_ts": None,
    }
    await record_telemetry_events([row])


def _spawn(detail: dict, ctx: dict) -> None:
    """Schedule the telemetry insert as a fire-and-forget task that never raises.

    INVARIANT: a telemetry failure MUST NOT break a tool call. The inner insert is
    wrapped so any exception is logged + swallowed; the task is held in a module set
    and discarded on completion (success or failure). Why: this runs on the tool-call hot path, so a raise would 500 the call.

    The only callers run inside the FastAPI event loop (the async handler wrapper) so
    a running loop is always present; create_task therefore never raises RuntimeError.
    """
    async def _run() -> None:
        try:
            await _insert(detail, ctx)
        except Exception:
            logger.warning("agent_tool telemetry record failed", exc_info=True)

    task = asyncio.create_task(_run())
    _pending_telemetry.add(task)
    task.add_done_callback(_pending_telemetry.discard)


def record_agent_tool_call(detail: dict, ctx: dict) -> None:
    """Record one agent_tool/call telemetry row, fire-and-forget (never raises)."""
    _spawn(detail, ctx)


def _classify_failure(outcome: dict, e: Exception) -> None:
    """Bucket a raised handler error into the telemetry outcome (re-raise is the
    caller's job — the agent must still see the validator error)."""
    if isinstance(e, HTTPException):
        outcome.update(ok=False, status=e.status_code, detail=str(e.detail)[:500])
    else:  # unexpected → server_error bucket
        outcome.update(ok=False, status=500, detail=repr(e)[:500])


def track_agent_tool(tool_name: str):
    """Decorator: record one agent_tool/call telemetry row around a Tool-API handler.

    MUST be placed ABOVE the handler (closest to the function) so it wraps the bare
    function BEFORE FastAPI's @router.post sees it — i.e. stack as:

        @router.post("/edit_document")
        @track_agent_tool("edit_document")
        async def tool_edit_document(...): ...

    functools.wraps preserves the handler's signature so FastAPI's Depends / Body
    dependency injection resolves unchanged (inspect.signature follows __wrapped__).
    The wrapped handler is timed; on HTTPException it captures status+detail, then
    re-raises so the agent still sees the validator error. A telemetry write happens
    in the finally (fire-and-forget) regardless of outcome.

    """

    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            ctx = kwargs.get("ctx") or {}
            body = kwargs.get("body")
            t0 = time.perf_counter()
            outcome = {"ok": True, "status": None, "detail": None, "result_status": None}
            try:
                result = await fn(*args, **kwargs)
                if isinstance(result, dict):
                    outcome["result_status"] = result.get("status")
                    outcome["result_shape"] = result_shape_of(result)
                return result
            except Exception as e:
                _classify_failure(outcome, e)
                raise
            finally:
                _record_call(tool_name, body, outcome, t0, ctx)

        return wrapper

    return deco


def _record_call(tool_name: str, body, outcome: dict, t0: float, ctx: dict) -> None:
    """Fire-and-forget telemetry row for one finished tool call (any outcome)."""
    latency_ms = int((time.perf_counter() - t0) * 1000)
    record_agent_tool_call(build_detail(tool_name, body, outcome, latency_ms, ctx), ctx)

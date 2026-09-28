"""Tool-API route GENERATION from the agent_tools registry.

# SYSTEM: agent-tools — the Tool-API POST routes are generated, never
# hand-decorated — the registry is the ONE tool surface, and it replaces the
# per-domain @router.post decorators.

# ARCH: the generated
# wrapper carries NO hold — the mid-turn approval ask lives in the DRIVER
# (dsh's user-approval service; the handlers refuse a confirm cell with
# 409 {code: confirmation_required} and the driver retries after asking).
# The wrapper keeps exactly one cross-cutting gate every mutating call must
# pass regardless of handler: the pin scope.

# INVARIANT: the generated route path MUST equal the tool name exactly.
# Why: the dsh driver builds the URL generically
# (`${TOOL_API_INTERNAL_URL}/api/tool/${toolName}`)
# and consults no path map — a mismatch is a runtime 404 the suite catches only
# by asserting over the served list (test_agent_tools_registry totality +
# test_tool_api_routes_match_tool_names). This held as a per-route comment on
# the hand-decorated routes; generating `/{entry.name}` makes it structural.

# WHY: the generated endpoint preserves the handler's __name__ and __doc__:
# FastAPI derives the OpenAPI operation id from the endpoint function name and
# the description from its docstring; the exported openapi.json is the tracked
# surface contract, so a generated `endpoint42` name would churn the published
# operation ids for zero functional change.
"""
# NOTE: deliberately NO `from __future__ import annotations` here. The
# generated endpoint's `body: model` annotation must evaluate to the REAL
# pydantic class at def time (the closure variable), not stay a string
# ForwardRef — with the future import, FastAPI/pydantic see ForwardRef('model')
# and every request dies in TypeAdapter at validation time.
from agent.context import get_agent_context, pinned_session_doc_id
from fastapi import HTTPException

from agent_tools import registry
from agent_tools.registry import _is_region_capable


def _make_endpoint(entry, handler, model, body_param, ctx_dep):
    """Build one FastAPI endpoint closure over the registry handler.

    The wrapper runs the pin-scope gate, then the handler — the confirm cell
    (a mutating call without approval) refuses INSIDE the handler with the
    machine-readable ask signal, so the driver's approval retry re-enters this
    endpoint with the X-Agent-Verdict marker and applies.

    `body: model` binds the pydantic annotation at def time; `body_param` is
    Body(...) for required bodies or Body(default_factory=model) for
    all-optional models (the one pre-registry case: get_project_structure).
    """

    async def endpoint(
        body: model = body_param,
        ctx: dict = ctx_dep,
    ):
        await _enforce_pin_scope(entry, body, ctx)
        # KEYWORD call: the @track_agent_tool wrapper reads `body`/`ctx` from
        # kwargs (its telemetry row + halt-directive carrier depend on them).
        return await handler(body=body, ctx=ctx)

    endpoint.__name__ = handler.__name__
    endpoint.__doc__ = handler.__doc__
    return endpoint


async def _enforce_pin_scope(entry, body, ctx) -> None:
    """Refuse a mutating call that escapes the session's pinned region.

    # INVARIANT(security): under a pin, a mutating call against the PINNED document
    # is allowed only if the tool is region-capable AND carries the region.
    # Why: the pin is the user's promise that the agent touches one fragment and
    # nothing else in that document. Two ways it was escapable: omitting `region`
    # from the body (the gate read capability off the body, so absence read as
    # "unpinned"), and the table tools, which mutate the same document's Y subtree
    # but carry no region at all and so were never checked. Both are refused here,
    # in the GENERATED wrapper, so a third-party tool cannot omit itself — the same
    # placement argument the deleted mid-turn hold once made.
    #
    # A call against a DIFFERENT document is not what the user pinned and is left
    # alone: the pin narrows one document, it is not a session-wide freeze.
    """
    if not entry.mutating:
        return
    pinned_doc_id = await pinned_session_doc_id(ctx)
    if not pinned_doc_id:
        return
    if getattr(body, "document_id", None) != pinned_doc_id:
        return
    if not _is_region_capable(entry):
        raise HTTPException(status_code=409, detail={
            "code": "region_out_of_scope",
            "detail": (
                f"{entry.name} is outside the pinned text region — this session is "
                "pinned to a fragment, and this tool cannot be confined to it. "
                "Ask the user to unpin the selection first."
            ),
        })
    if getattr(body, "region", None) is None:
        raise HTTPException(status_code=409, detail={
            "code": "region_required",
            "detail": (
                "This session is pinned to a text fragment, so an edit must carry "
                "the pinned region. Re-send the call with `region`, or ask the user "
                "to unpin the selection."
            ),
        })


def bind_tool_api_routes(router) -> None:
    """Generate one POST /{name} route per registry entry with the tool_api
    surface, in declaration order. Called by routes/tool_api/__init__.py AFTER
    the domain modules are imported (their handlers must exist for
    resolve_handler)."""
    from fastapi import Body, Depends

    for entry in registry.tool_api_entries():
        handler = registry.resolve_handler(entry)
        model = entry.request_model
        if model.model_fields and all(
            not f.is_required() for f in model.model_fields.values()
        ):
            body_param = Body(default_factory=model)
        else:
            body_param = Body(...)
        endpoint = _make_endpoint(
            entry, handler, model, body_param, Depends(get_agent_context),
        )
        router.add_api_route(f"/{entry.name}", endpoint, methods=["POST"])

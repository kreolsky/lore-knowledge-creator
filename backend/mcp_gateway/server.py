"""MCP gateway server — lowlevel Server, ASGI auth gate, lifespan wiring.

# The streamable-HTTP MCP endpoint (mounted at /mcp). see SYSTEM: mcp-gateway.

# ARCH: a lowlevel mcp.server.lowlevel.Server (NOT FastMCP) so tool
# schemas come programmatically from AGENT_TOOLS via schemas.build_tool_list
# (the single source of truth) — FastMCP decorators would duplicate it.
# StreamableHTTPSessionManager(stateless=True, json_response=True) sits behind
# the mounted /mcp ASGI app: stateless ⇒ each POST is a standalone, auto-
# initialized session (no SSE, no separate initialize handshake), which fits
# per-call Bearer re-auth and survives restarts/workers.

# INVARIANT (two auth gates):
#   1. ASGI 401 gate (cheap, no DB) — rejects a missing/malformed Authorization
#      header BEFORE the session manager runs. Runs for every /mcp request.
#   2. per-call gate (DB-backed) — inside call_tool AND list_tools, read the
#      header via the handler context's `ctx.request` and run
#      authenticate_agent_token (hash lookup, expiry, rate limit, live
#      project-membership re-check). No result is cached across calls.
#      list_tools renders the tool texts for the key's OWN root, so it can never
#      fall back to the unscoped list on failure — the refusal is a JSON-RPC
#      error carrying the 401/403 detail.

# ARCH (lifespan): Starlette does NOT run a mounted sub-app's lifespan, so the
# session manager's run() context is entered via mcp_lifespan() from the host
# app's lifespan (main.py). It closes BEFORE the rest of shutdown (LIFO) so the
# gateway's task group tears down first.
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from agent_tools.registry import REGISTRY
from api_key_auth import authenticate_agent_token
from fastapi import HTTPException
from jsonschema import exceptions as jsonschema_exceptions
from jsonschema.validators import validator_for
from mcp import types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from rate_limit import check_mcp_mutating_rate_limit, check_mcp_transport_rate_limit

from db import coerce_record_ids, fetch_one
from mcp_gateway.dispatch import dispatch_tool
from mcp_gateway.schemas import (
    _served_mcp_names,
    build_tool_list,
    preview_extractor_visible,
)

logger = logging.getLogger(__name__)

_server: Server | None = None
_manager: StreamableHTTPSessionManager | None = None
_input_validators: dict[str, Any] = {}


def build_server() -> Server:
    """Construct (once) the lowlevel MCP gateway server + session manager.

    Idempotent: the SessionManager can only run() once per instance, so the server
    and its manager are module singletons built on first call (at import of
    main.py) and reused.
    """
    global _server, _manager
    if _server is None:
        # `instructions` surfaces in the InitializeResult, so a harness learns
        # the bootstrap path before it even lists tools: call `init` first.
        server = Server(
            "lore-gateway",
            instructions=(
                "Call the `init` tool ONCE before anything else. It returns the "
                "full operating package: tool protocol, editing/error contract, "
                "the project's rules, config indexes, and your work area."
            ),
            on_list_tools=_list_tools,
            on_call_tool=_on_call_tool,
        )
        _manager = StreamableHTTPSessionManager(
            app=server, json_response=True, stateless=True,
        )
        _server = server
    return _server


async def _list_tools(
    ctx: ServerRequestContext, params: types.PaginatedRequestParams | None,
) -> types.ListToolsResult:
    """tools/list under gate 2. The SDK calls this only for a real listing —
    tools/call never refreshes through it, so there is no unauthenticated path."""
    # AUTH (gate 2 for the listing): same Bearer auth as tools/call — the
    # texts served here are RENDERED for the key's own root, so a failed
    # auth must refuse, never fall back to the unscoped list (that wording
    # is the lie the render exists to remove). resolve_api_key ticks the
    # general bucket and stamps last_used_at — a listing IS a use.
    try:
        token = _bearer_token(ctx.request)
        key_ctx = await authenticate_agent_token(token)
    except HTTPException as exc:
        raise _jsonrpc_error(exc) from exc
    scope_root = key_ctx.get("scope_root") or ""
    root_title = ""
    if scope_root:
        # A deleted scope root renders an empty title — the same expression
        # init's capabilities.scope uses, consistency over a new error path.
        root_doc = await fetch_one("documents", scope_root)
        root_title = (root_doc or {}).get("title") or ""
    return types.ListToolsResult(tools=build_tool_list(
        scope_root=scope_root, root_title=root_title,
        preview_visible=await preview_extractor_visible(),
    ))


async def _on_call_tool(
    ctx: ServerRequestContext, params: types.CallToolRequestParams,
) -> types.CallToolResult:
    return await _call_tool(ctx.request, params.name, params.arguments or {})


def _bearer_token(request) -> str:
    """Read the Bearer token off the handler context's HTTP request. The ASGI
    gate (1) already rejected missing/malformed headers, so this is the same
    defensive mirror _call_tool has always had. A context with no request (a
    non-HTTP transport) raises the same 401 as a malformed header, so every
    caller has exactly ONE failure type to handle."""
    auth = request.headers.get("authorization", "") if request is not None else ""
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization")
    return auth[len("Bearer "):]


def _jsonrpc_error(exc: HTTPException) -> MCPError:
    """An HTTPException from the listing's auth → a JSON-RPC error the client
    can read: message = the HTTP detail, data.status_code = the HTTP status.
    Raised as MCPError because the lowlevel server maps exactly that to a
    JSON-RPC error response (a bare exception would collapse to code 0 with a
    str() message and lose the structure)."""
    return MCPError(
        code=-32000,
        message=str(exc.detail),
        data={"status_code": exc.status_code},
    )


async def _call_tool(request, name: str, arguments: dict) -> types.CallToolResult:
    """Dispatch one tool call under per-call Bearer auth.

    Gate (2): read the Authorization header from the live request context and
    authenticate the agent token (the ASGI gate already rejected missing/
    malformed headers, so this is the DB-backed validity + RBAC check).
    """
    try:
        token = _bearer_token(request)
        ctx = await authenticate_agent_token(token)
        # The mint must return an ABSOLUTE url
        # (scheme from X-Forwarded-Proto, host from X-Forwarded-Host || Host). uvicorn
        # does NOT run with --proxy-headers, so request.base_url would yield http://
        # behind a TLS-terminating proxy — derive explicitly from forwarded headers.
        ctx["base_url"] = _public_base_url(request)
        await _validate_input(name, arguments)

        # ONE routing table: the agent_tools registry. A name it does not serve
        # on the mcp surface (or a deployment-gated one) 404s inside
        # dispatch_tool — advertised == routed is structural (schemas reads the
        # same table for tools/list).
        entry = REGISTRY.get(name)
        if entry is not None and entry.mutating:
            # Mutating-write rate limit (30/60s per key) — by EFFECT (the entry's
            # `mutating` flag covers the base tools AND the write-by-effect
            # gateway tools attach_file / reprocess_file, whose mint IS the
            # write). Placed AFTER auth (ctx["key_id"] is available) and BEFORE
            # dispatch so a trip raises 429, which _err() maps to an MCP isError
            # result the agent backs off from. Read-only calls are never
            # throttled by this tier.
            if not await check_mcp_mutating_rate_limit(ctx["key_id"]):
                raise HTTPException(status_code=429, detail="Mutating tool rate limit exceeded")
        result = await dispatch_tool(name, arguments, ctx)
        # Structured audit log of every successful dispatch. No token material is
        # logged — key_id is an internal id (the plaintext secret is never in ctx
        # and never persisted). Failed dispatches raise HTTPException → _err() and
        # are not logged here; unexpected failures go through logger.exception.
        status = result.get("status") if isinstance(result, dict) else "ok"
        logger.info(
            "mcp.call",
            extra={
                "tool": name,
                "key_id": ctx["key_id"],
                "project_id": ctx["project_id"],
                "status": status,
            },
        )
        return _ok(result)
    except HTTPException as exc:
        return _err(exc)
    except Exception:  # noqa: BLE001 — surface any failure as an MCP tool error
        logger.exception("MCP call_tool '%s' failed", name)
        return _err(HTTPException(status_code=500, detail="Internal gateway error"))


async def _validate_input(name: str, arguments: dict) -> None:
    """Refuse arguments that do not match the served tool's inputSchema — a 422
    whose message names the violated constraint, before dispatch.

    # ARCH: the mcp SDK (from 2.0) no longer validates tools/call arguments
    # against Tool.inputSchema on the server side; the gateway keeps the contract
    # that a schema-invalid call (a missing required arg, a non-integer window)
    # is refused BEFORE dispatch. It runs where the SDK's check ran — above
    # dispatch_tool, whose direct callers get no validation — but after auth, so
    # an unauthenticated caller cannot probe schemas. A tool not served on this
    # deployment is skipped and 404s in dispatch, as it did when it had no
    # schema. Validators compile once: properties are static registry text
    # (only descriptions are rendered per key).
    """
    if name not in _served_mcp_names(preview_visible=await preview_extractor_visible()):
        return
    if not _input_validators:
        for tool in build_tool_list(preview_visible=True):
            _input_validators[tool.name] = validator_for(tool.input_schema)(tool.input_schema)
    validator = _input_validators.get(name)
    if validator is None:
        return
    error = jsonschema_exceptions.best_match(validator.iter_errors(arguments))
    if error is not None:
        refusal = HTTPException(status_code=422, detail=f"Input validation error: {error.message}")
        refusal.next_action = "fix the arguments per the tool's inputSchema and retry"
        raise refusal


def _public_base_url(request) -> str:
    """Absolute base URL from forwarded headers. scheme from X-Forwarded-Proto,
    host from X-Forwarded-Host || Host (first value if a comma list). Empty when no
    host is resolvable (tests/local). A relative `path` sibling is returned alongside
    the absolute url by the mint so a client that knows better can ignore it."""
    if request is None:
        return ""
    headers = request.headers
    scheme = (headers.get("x-forwarded-proto") or request.url.scheme or "http")
    scheme = scheme.split(",")[0].strip()
    host = (headers.get("x-forwarded-host") or headers.get("host") or "")
    host = host.split(",")[0].strip()
    return f"{scheme}://{host}" if host else ""


def _ok(result: Any) -> types.CallToolResult:
    """Success: the executor's JSON-serializable result as a text block (legacy
    back-compat — older clients read JSON out of `content[0].text`) AND as a typed
    `structuredContent` dict (capable clients validate against Tool.outputSchema).

    # ARCH (canonical ids): every id the gateway emits is normalized to a plain
    # uuid HERE — the single output boundary — via db.coerce_record_ids (the
    # recursive, type-based RecordID walk; see its F5 ARCH note in db.records). The
    # detector is shared with db.serialize_record so the two boundaries can't drift.

    # ARCH (text ↔ structured parity): one JSON round-trip (default=str, so
    # datetimes serialize) feeds BOTH the text block and structuredContent, so a
    # capable client's structuredContent deep-equals the legacy text a client that
    # ignores structuredContent still parses — the two surfaces can never drift.
    """
    result = coerce_record_ids(result)
    text = json.dumps(result, default=str)
    structured = json.loads(text) if isinstance(result, dict) else None
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent=structured,
        isError=False,
    )


# Ergonomics #4 (weak-model plan): every error result carries an explicit
# `next_action` instruction so a weak model FOLLOWS a directive rather than
# inferring behavior from the status code. Codes are unchanged.
_NEXT_ACTION_BY_STATUS: dict[int, str] = {
    400: "stop",              # malformed args — a blind retry repeats the error
    401: "stop",              # invalid/expired key — report to operator
    403: "stop",              # access denied / read-only key — do not retry
    404: "verify the id",       # not found — re-reading the same id can't succeed
    409: "re-read then retry",  # stale match — re-read the current text
    422: "split into smaller edits",  # full_rewrite — old_string too large; break it up

    429: "back off ~60s",     # rate limit — wait then retry
}


def _err(exc) -> types.CallToolResult:
    """Error: {error, status_code, next_action} as an isError text block (plan:
    HTTPException → MCP tool error result). Uniform-404 semantics are preserved
    automatically because the dispatch path reuses the same executors as the
    Tool-API. `next_action` (ergonomics #4) tells a weak model what to do next;
    an executor can pin it on the raised exception when the status-keyed map
    mislabels the refusal — 422 serves both the full_rewrite split advice and
    argument-fix refusals (include_outline's start_id)."""
    detail = exc.detail if hasattr(exc, "detail") else str(exc)
    status_code = exc.status_code if hasattr(exc, "status_code") else 500
    # FastAPI detail can be a dict/list — keep it structured under `error`.
    payload = {
        "error": detail,
        "status_code": status_code,
        "next_action": getattr(exc, "next_action", None)
        or _NEXT_ACTION_BY_STATUS.get(status_code, "stop"),
    }
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, default=str))],
        isError=True,
    )


# ─── ASGI auth gate + mount point ─────────────────────────────────────────────


async def _send_http_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    headers = [
        [b"content-type", b"application/json"],
        [b"content-length", str(len(body)).encode()],
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def mcp_asgi_app(scope, receive, send) -> None:
    """The mounted /mcp ASGI app: a cheap Authorization format gate in front of
    the session manager's handle_request (gate 1; the DB-backed gate 2 runs per
    tool call inside call_tool)."""
    build_server()
    # Only POST reaches the JSON-RPC endpoint in stateless+json mode.
    if scope.get("type") == "http":
        # WHY: in stateless mode we reject the standalone GET SSE stream with
        # 405 (before the auth gate — method-not-allowed is not credential-dependent
        # and avoids a DB touch). Why: the SDK's _handle_get_request opens an
        # unbounded keep-alive SSE stream even under stateless+json_response (that
        # flag only shapes POST); with no session we never push server→client
        # notifications, so the stream is dead weight that hangs forever and trips
        # the prod reverse-proxy read-timeout into a spurious 5xx on connect. This
        # is a repeatedly-regressing transport contract — a future feature that DOES
        # push server-initiated notifications would need the GET stream back and must
        # revisit this branch. Symmetric with the SDK's own 405 on DELETE.
        if scope.get("method") == "GET":
            await _send_http_json(
                send, 405, {"detail": "SSE stream not supported in stateless mode"}
            )
            return
        headers = scope.get("headers") or []
        auth = ""
        for k, v in headers:
            if k == b"authorization":
                auth = v.decode("latin-1")
                break
        token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
        if not token:
            await _send_http_json(send, 401, {"detail": "Missing or invalid Authorization"})
            return
        # F3 (audit): transport-level flood ceiling BEFORE the stateless session
        # manager runs. tools/list now carries per-call DB auth (the {{ROOT}}
        # render needs the key), but initialize and ping still reach the manager
        # with no app-level auth — without this ceiling an unauthenticated flood
        # (bad token spam) hits the manager unbounded. Keyed on the token hash —
        # the cheap gate never touches the DB.
        import hashlib

        token_hash = hashlib.sha256(token.encode()).hexdigest()
        if not await check_mcp_transport_rate_limit(token_hash):
            await _send_http_json(send, 429, {"detail": "Too many requests"})
            return
    await _manager.handle_request(scope, receive, send)  # type: ignore[union-attr]


class McpASGIApp:
    """Callable ASGI wrapper so Starlette `Route` (not `Mount`) hosts the gateway.

    # ARCH: the gateway is mounted at the EXACT path /mcp (and /mcp/). A `Mount`
    # would 307-redirect the bare /mcp → /mcp/ (Router redirect_slashes), which
    # some MCP HTTP clients do not follow. A Route matches the path exactly with
    # no redirect. Starlette `Route` treats a non-function callable as an ASGI
    # app, so this wrapper (rather than a bare `async def`) is forwarded as-is.
    """

    async def __call__(self, scope, receive, send) -> None:
        await mcp_asgi_app(scope, receive, send)


@asynccontextmanager
async def mcp_lifespan():
    """Enter the session manager's run() context. Called from main.py's lifespan
    (ASGITransport + mounted sub-apps skip the app lifespan). LIFO close ⇒ the
    manager tears down before the rest of shutdown.

    The teardown is made robust: run() cancels its internal task group on exit,
    which surfaces a cancelled-task exception group when in-flight request tasks
    are torn down. That is benign on shutdown (the manager is being destroyed), so
    it is logged rather than propagated — true process cancellation still
    propagates. Startup errors are NOT swallowed (let them fail loudly).
    """
    import anyio

    build_server()
    assert _manager is not None
    run_ctx = _manager.run()
    await run_ctx.__aenter__()  # startup — propagate failures loudly
    try:
        yield
    finally:
        try:
            await run_ctx.__aexit__(None, None, None)
        except BaseException as exc:  # noqa: BLE001 — shutdown teardown only
            if isinstance(exc, anyio.get_cancelled_exc_class()):
                raise
            logger.debug(
                "MCP session manager teardown surfaced a benign exception group "
                "(expected on shutdown)", exc_info=True,
            )


__all__ = ["build_server", "mcp_asgi_app", "McpASGIApp", "mcp_lifespan"]

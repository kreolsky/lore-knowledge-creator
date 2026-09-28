"""MCP gateway-only tool handlers — init, preview_extractor, get_file,
attach_file, reprocess_file (the tools that exist ONLY on the gateway surface).

# Gateway-only tool implementations. see SYSTEM: mcp-gateway.

# ARCH: these are REGISTRY entries
# (agent_tools.entries_gateway — surfaces={"mcp"}, request_model None); the
# registry names each per-tool handler below and mcp_gateway's adapter calls it
# as handler(args, ctx). The former dispatch_gateway_only name-router is gone —
# routing lives in the ONE registry, not in a gateway-local chain.
# The signed-URL token transports this module's file tools mint against live in
# mcp_gateway/upload.py + download.py.
# Import DAG is one-way: mcp_gateway → routes.* / files_service / pipeline.*,
# NEVER the reverse.

# INVARIANT: every dispatch path operates under `ctx` (the authenticated agent
# context); write-by-effect tools re-run dispatch._require_writable at mint.
# Why: the agent must never exceed the owning user's rights — auth is per-call,
# and minting IS the write for attach_file/reprocess_file (their tokens act
# without a key), so they take the same binary-key-model gate as the mutating
# Tool-API tools.
"""
from __future__ import annotations

import asyncio

from fastapi import HTTPException

from models import is_ref_row

# Concrete MIME for an upload kind when `mime_for_filename` comes up empty (the
# stdlib mimetypes DB lacks .docx in the slim container image). Used only to keep
# the token well-formed; the real type is confirmed by validate_magic at redeem.
_MIME_FOR_KIND: dict[str, str] = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "markdown": "text/markdown",
    "audio": "audio/mpeg",
    "image": "image/png",
}


def _poll_after_s(kind: str, max_mb: int) -> int:
    """Coarse 'first poll after N seconds' heuristic from media type + size cap.

    NOT a promise — it just turns blind polling into bounded polling. Audio
    transcription is the slow path; docx conversion is fast; images have no derived
    text at all (the agent learns 'nothing to wait for' from processing_status=null)."""
    if kind == "audio":
        return 30 if max_mb <= 50 else 120
    if kind == "docx":
        return 15
    return 5

# Concurrency cap for the extractor dry-run (preview_extractor). A single local-LLM
# run is ~50-70s and the model serves one request at a time, so N concurrent
# preview_extractor calls would hold N web connections while stacked on the serial
# LLM queue. The semaphore mirrors the LLM's own single-queue: concurrent callers
# serialize instead of piling up. A count-based per-key rate limit is
# intentionally NOT applied here — the benchmark legitimately runs many refs
# serially (21+), which a count cap would reject; the concurrency cap removes the
# real DoS vector (connection/queue pile-up) without breaking that use.
_extractor_concurrency = asyncio.Semaphore(1)


async def tool_init(_args: dict, ctx: dict) -> dict:
    """Registry handler for `init` — the bootstrap package assembler (the
    uniform (args, ctx) gateway-handler shape; init takes no arguments)."""
    from mcp_gateway.bootstrap import build_init_package

    return await build_init_package(ctx)


async def _authorize_upload_mint(document_id: str, ctx: dict) -> None:
    """Run every AUTHORIZATION check the upload mint depends on. Raises, never returns
    a decision (arg shape is the caller's job).

    # INVARIANT(security): EVERY authorization decision for a signed upload happens
    # HERE, at mint time. Why: the redeem route has no key to check, so anything it
    # could still decide would be decided by whoever holds the URL — therefore the
    # full chain (read-write key, project match, host document exists, full project
    # access, subtree scope) must run NOW, under the per-call Bearer key, and the
    # caller freezes the outcome into the token's claims. Mirrors the download tool,
    # which likewise binds authorization at mint. Kept as ONE function so the chain
    # cannot be partially applied by a future second mint path.
    """
    from access import get_project_access
    from db import fetch_one
    from mcp_gateway.dispatch import _require_writable
    from scope import require_doc_in_scope

    # A read-only key must not mint: minting IS the write here, because the redeem
    # route accepts the token alone. Same gate dispatch_tool applies to every
    # entry the registry declares mutating.
    _require_writable(ctx)

    # Host document: uniform 404 on cross-project (no existence oracle), the same
    # shape resolve_reference_file returns for a foreign reference.
    host = await fetch_one("documents", document_id)
    if not host or host.get("project_id") != ctx["project_id"] or host.get("deleted_at"):
        raise HTTPException(status_code=404, detail="Document not found")
    # A file attaches to a
    # DOCUMENT, not to another attached node. Why refused at mint: the schema event
    # documents_parent_check (surreal/schema.surql) forbids a reference from being a
    # parent, so a redeem against a reference host 500s at create_record AFTER the
    # client streamed the whole file — and save_upload writes bytes BEFORE the
    # insert, so each attempt orphans a directory nothing reaps. Checking here (mint,
    # under the per-call Bearer key) refuses before a URL is ever handed out.
    # Reachability is not theoretical: read_document returns references[], so an
    # agent routinely holds reference-node ids that look exactly like host ids. The
    # message names the fix in the surface's own vocabulary (it deliberately dropped
    # the word "reference"), NOT the schema event's internal text — an agent reading
    # "Reference documents cannot be parents" has nothing actionable.
    if is_ref_row(host):
        raise HTTPException(
            status_code=400,
            detail="A file attaches to a document, not to another attached node — "
                   "pass the host document's id as attach_to",
        )
    if await get_project_access(ctx["project_id"], ctx["user"]) != "full":
        raise HTTPException(status_code=403, detail="Full project access required to create")
    await require_doc_in_scope(ctx.get("scope_root"), document_id)


async def _dispatch_attach_file(args: dict, ctx: dict) -> dict:
    """Mint a short-lived signed URL the client POSTs a file to — the ONE byte
    channel: no tool accepts bytes as an argument at any size, and every format
    goes through this one channel, always as a URL. Authorization: see _authorize_upload_mint's
    INVARIANT. Token model + jti replay-safety: see mcp_gateway/upload.py.
    """
    from mcp_gateway.upload import mint_upload_token

    filename, attach_to, mime, kind, max_mb = await _resolve_attach_inputs(args)
    await _authorize_upload_mint(attach_to, ctx)

    title = str(args.get("title") or "").strip() or filename
    token, expires_iso, _jti = await mint_upload_token(
        project_id=ctx["project_id"], document_id=attach_to, filename=filename,
        title=title, mime=mime, user_id=ctx["user_id"],
        # S1: the redeem route has no key — the making key's label rides the token
        # (authorization-bound facts are frozen at mint, scope included).
        agent_label=ctx.get("key_label"),
    )
    return await _build_attach_response(filename, attach_to, kind, max_mb, token, expires_iso, ctx)


async def _resolve_attach_inputs(args: dict):
    """Validate filename + attach_to and decide the media branch from the extension
    NOW (audio/image/docx/markdown), so an unsupported type fails before the
    client streams anything to a URL that would 400 at the end."""
    from files_util import classify_upload_kind, mime_for_filename

    filename = str(args.get("filename", "")).strip()
    attach_to = str(args.get("attach_to", "")).strip()
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")
    if not attach_to:
        raise HTTPException(
            status_code=400,
            detail="attach_to is required — a file node attaches to a host document",
        )

    mime = mime_for_filename(filename)
    kind, max_mb = await classify_upload_kind(filename, mime)
    if kind is None:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {mime or filename}.")
    # `mime_for_filename` can return "" (mimetypes lacks some extensions, e.g. .docx
    # in the container) — but verify_upload_token REQUIRES a non-empty mime, and the
    # magic check at redeem keys off it. Derive a concrete mime from the kind when the
    # sniffer came up empty, so the token is well-formed for every accepted format.
    if not mime:
        mime = _MIME_FOR_KIND[kind]
    return filename, attach_to, mime, kind, max_mb


async def _build_attach_response(filename: str, attach_to: str, kind: str, max_mb: int,
                                  token: str, expires_iso: str, ctx: dict) -> dict:
    """Assemble the mint response.

    # The node id is born at REDEEM, not here, so the mint
    # echoes `attach_to` (the host) and NEVER `document_id` (which appears only in
    # the redeem response). The URL is ABSOLUTE (scheme+host from forwarded headers in
    # ctx["base_url"]); a relative `path` sibling lets a client that knows better
    # ignore `url`. `example` is executable as-written and its stdout IS the redeem JSON.
    """
    import settings

    from mcp_gateway.upload import MCP_UPLOAD_ROUTE_PREFIX

    # Echoes the TTL the mint just used, read through settings so the response
    # never disagrees with the token's own exp.
    ttl = await settings.get("MCP_UPLOAD_TOKEN_TTL_S")
    path = MCP_UPLOAD_ROUTE_PREFIX + token
    base = ctx.get("base_url") or ""
    url = base + path if base else path
    example = f'curl -X POST "{url}" -F "file=@{filename}"'
    return {
        "url": url,
        "path": path,
        "method": "POST",
        "field_name": "file",
        "filename": filename,
        "attach_to": attach_to,
        "accepted": True,
        "max_size_mb": max_mb,
        "expires_in_s": ttl,
        "expires_at": expires_iso,
        "poll_after_s": _poll_after_s(kind, max_mb),
        "example": example,
    }


async def _dispatch_get_file(args: dict, ctx: dict) -> dict:
    """Mint a short-lived signed URL to download a node's stored original file:
    an absolute url, symmetric with attach_file — a URL, never bytes.

    Runs under per-call Bearer auth: resolve_reference_file verifies the agent key's
    project owns the node (uniform 404 on a cross-project id) BEFORE minting, so a
    bad id 404s immediately rather than producing a dead URL. The serve route is
    UNAUTHENTICATED: the signed JWT {ref_id, exp} is the authorization, and it
    re-resolves the file by ref_id alone (project_id=None — the token already bound it).

    # INVARIANT (security): the token authorizes exactly one ref_id and expires; it
    # is not a session and grants nothing else.
    # Why: the TTL, the single-ref_id binding and the required `exp` are what keep
    # the URL from becoming a general file oracle — none of the three is optional.
    """
    from files_service import resolve_reference_file

    from mcp_gateway.download import mint_download_token

    document_id = str(args.get("document_id", "")).strip()
    if not document_id:
        raise HTTPException(status_code=400, detail="document_id is required")
    # Verify ownership + resolve the stored name (a 404 here is the honest signal;
    # minting a URL for a missing/fileless node would just 404 on fetch).
    _abs, _mime, safe_name = await resolve_reference_file(
        ref_id=document_id, project_id=ctx["project_id"],
    )
    # WHY (security): confine to the agent KEY's subtree scope, like every
    # other node-access tool. Without it a subtree-scoped key could download a binary
    # from OUTSIDE its subtree but inside the project — an intra-project scope escape.
    from scope import require_doc_in_scope

    await require_doc_in_scope(ctx.get("scope_root"), document_id)
    token, expires_iso = await mint_download_token(document_id)
    path = f"/api/mcp/download/{token}"
    base = ctx.get("base_url") or ""
    url = base + path if base else path
    return {
        "document_id": document_id,
        "url": url,
        "filename": str(args.get("filename") or "") or safe_name,
        "expires_at": expires_iso,
    }


async def _dispatch_reprocess_file(args: dict, ctx: dict) -> dict:
    """Re-run a node's conversion, gated to the stuck/failed state.

    The shared `_validate_reprocessable` accepts error|ready|processing and is shared
    with the app's REST retry endpoint (an escape hatch). The MCP gate subtracts
    `ready` AND the non-empty-text case: MCP is force-auto with no confirmation tier,
    so an ungated call could destroy a human-corrected transcript irreversibly (binary
    nodes are not covered by the History panel — `_reprocess_reference` wipes content).
    `processing` is INSIDE the gate: a node stuck at processing has no text to wipe, so
    the reason for refusing simply does not apply (a dead worker is the case manual
    retry exists for). The gate leaves ONE real dead end — error + non-empty text — and
    names the human exit verbatim so the agent reports a next action, not a blind retry.

    # WHY: this is an MCP-EDGE check. `_validate_reprocessable` is byte-identical
    # (asserted unchanged by test_d7_shared_validate_reprocessable_is_unchanged).
    # Why: MCP is force-auto (no confirmation tier) — the narrowing stays HERE so the app's REST retry escape hatch stays intact in the shared helper.
    """
    from db import fetch_one
    from mcp_gateway.dispatch import _require_writable
    from scope import require_doc_in_scope

    _require_writable(ctx)
    document_id = str(args.get("document_id", "")).strip()
    if not document_id:
        raise HTTPException(status_code=400, detail="document_id is required")
    ref = await fetch_one("documents", document_id)
    if not ref or ref.get("deleted_at") or not is_ref_row(ref):
        raise HTTPException(status_code=404, detail="Document not found")
    if ref.get("project_id") != ctx["project_id"]:
        raise HTTPException(status_code=404, detail="Document not found")
    await require_doc_in_scope(ctx.get("scope_root"), document_id)

    status = ref.get("processing_status")
    has_text = bool((ref.get("content") or "").strip())
    if status not in ("error", "processing"):
        raise HTTPException(
            status_code=400,
            detail="reprocess_file is only for a node whose processing_status is "
                   "'error' or 'processing' and whose text is empty.",
        )
    if has_text:
        raise HTTPException(
            status_code=403,
            detail="This node has text that would be wiped; retry it from the "
                   "document's reference panel in the app.",
        )
    from files_service import reprocess_reference

    result = await reprocess_reference(document_id, ref, user_id=ctx["user_id"])
    return {"status": "applied", "document_id": document_id, **result}


async def _dispatch_preview_extractor(args: dict, ctx: dict) -> dict:
    """Dry-run the extractor via the SAME resolver the editor uses.

    # ARCH: one resolution path. The tool calls
    # resolve_extractor_params (the helper extracted from run_agent) so the editor
    # and the benchmark can no longer resolve different model/config/source. The
    # flow runs synchronously in the web process (~50s of local LLM) — acceptable
    # for a dev/benchmark tool; the flow is async I/O so it does not block the loop.
    # Gated by the resolver's require_project_full (project-scoped agent key).
    # WHY: this DRY entry point never creates a document / emits —
    # run_extractor_dry is flow-only. Why: a side-effecting dry-run would corrupt the
    # benchmark baseline (spied in test_dry_run_returns_data_without_side_effects).
    # Scope: a property of this dispatcher, NOT of pipelines. A pipeline writes its own
    # output on the wet path — see the module docstring of pipeline.extractor.params.
    # Renamed run_extractor → preview_extractor (dry_run:false is rejected, so
    # the old name promised an action the tool cannot perform).
    """
    from pipeline.extractor.runner import run_extractor_dry

    params_list = await _resolve_extractor_params_list(args, ctx)
    params = _select_extractor_params(params_list, args)

    # Serialize heavy LLM runs (see _extractor_concurrency): the local model serves
    # one request at a time, so concurrent dry-runs queue here rather than pile up.
    async with _extractor_concurrency:
        result = await run_extractor_dry(params)
    # Echo the resolved inputs so a parity check can prove the tool ran the same
    # source/config/target/project/model the editor would have used.
    result["resolved"] = {
        "source_doc_id": params.source_doc_id,
        "config_doc_id": params.config_doc_id,
        "target_doc_id": params.target_doc_id,
        "project_id": params.project_id,
        "model": params.model,
    }
    return result


async def _resolve_extractor_params_list(args: dict, ctx: dict) -> list:
    """Validate inputs, reject wet runs, resolve params via the editor's resolver,
    then confine to the agent KEY's scope (project_id + subtree scope_root).

    # WHY (security): the resolver already carries the COMPLETE launch gate
    # (require_project_full — see the INVARIANT in pipeline.extractor.params). The two
    # checks below are a NARROWING that applies ONLY on this path, where the caller is
    # an agent KEY rather than a user: they bind to the key's project_id AND subtree
    # scope_root, so a project-A key cannot extract from a project-B reference even if
    # the owning user is a member of B. The key's scope is narrower than its owner's
    # membership — that is what these lines close, and nothing more. A surface with no
    # agent key (the agent chat, the editor) needs no equivalent and must not invent one.
    """
    from pipeline.extractor.params import resolve_extractor_params

    from scope import require_doc_in_scope

    reference_id = str(args.get("reference_id", "")).strip()
    if not reference_id:
        raise HTTPException(status_code=400, detail="reference_id is required")
    if args.get("dry_run", True) is False:
        raise HTTPException(
            status_code=400,
            detail="preview_extractor is dry-run only; a real run that creates a "
                   "document uses POST /api/agent-config/run",
        )

    # The resolver carries require_project_full — same gate as the editor. ctx["user"]
    # is the key owner (authenticated per-call by authenticate_agent_token).
    params_list = await resolve_extractor_params(reference_id, ctx["user"])
    if params_list[0].project_id != ctx["project_id"]:
        raise HTTPException(
            status_code=403,
            detail="Reference is outside this agent key's project scope",
        )
    await require_doc_in_scope(ctx.get("scope_root"), reference_id)
    return params_list


def _select_extractor_params(params_list: list, args: dict):
    """Disambiguate to a single config (by config_doc_id, erroring on multiples) and
    apply an optional model override."""
    from dataclasses import replace

    config_doc_id = args.get("config_doc_id")
    if config_doc_id:
        params_list = [p for p in params_list if p.config_doc_id == str(config_doc_id)]
        if not params_list:
            raise HTTPException(
                status_code=404,
                detail=f"No agent config with config_doc_id {config_doc_id} for this reference",
            )
    if len(params_list) > 1:
        raise HTTPException(
            status_code=400,
            detail="Multiple agent configs for this reference; pass config_doc_id to disambiguate",
        )

    params = params_list[0]
    model_override = args.get("model")
    if model_override:
        params = replace(params, model=str(model_override))
    return params


__all__ = ["tool_init"]

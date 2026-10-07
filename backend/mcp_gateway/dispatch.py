"""MCP gateway call adapter — the registry handler, adapted to MCP raw args.

# SYSTEM: mcp-gateway — call routing (see also SYSTEM: agent-tools).

# ARCH: MCP is a CLIENT of the Tool-API,
# not a second dispatcher. There is ONE routing table (agent_tools.registry) —
# this module no longer owns name sets or per-tool body builders; a call is
# resolved to the SAME in-process handler the registry names (the Tool-API
# route handler), so there is ONE code path per tool: selection-conflict, the
# live CRDT apply, pre-edit checkpoint and the soft-error mapping all live in
# routes.tool_api and are not duplicated here. Import DAG stays one-way:
# mcp_gateway → agent_tools / routes.*, NEVER the reverse.

# INVARIANT: every dispatch path operates under `ctx` (the authenticated agent
# context from api_key_auth), and executors re-check per-doc/per-project access.
# Why: the agent must never exceed the owning user's rights — auth is per-call.
"""
from __future__ import annotations

import logging

from agent_tools.registry import REGISTRY, resolve_handler
from fastapi import HTTPException
from inbox import KIND_REF, mark_arrived

from db import extract_id, fetch_one
from models import is_ref_row

logger = logging.getLogger(__name__)


# DEBT: weak-model arg tolerances — Why deferred: the advertised tool schemas
# (schemas._agent_tool_to_mcp) hide these legacy spellings so a capable model
# emits only the canonical keys, but the adapter still ACCEPTS them for
# back-compat with weak/legacy models. They are gathered here as ONE labeled
# member per spelling so a future tolerance is a new entry in this function,
# never a new `if` scattered across a dispatch body (name-the-class rule). When
# every model reliably emits canonical keys, this whole layer can be deleted.
# Exit criterion (measurable): TOLERANCE_HITS counts every time a tolerance
# actually FIRES — when it reads zero over a representative deployment window,
# the layer can be deleted. Mutated in place (never rebound), so a reader holding
# the dict observes live increments.
TOLERANCE_HITS: dict[str, int] = {}


def _tol_hit(key: str) -> None:
    """Record one tolerance firing (the DEBT's exit signal)."""
    TOLERANCE_HITS[key] = TOLERANCE_HITS.get(key, 0) + 1


def _normalize_args(name: str, args: dict) -> dict:
    """Fold aliased/legacy argument spellings into their advertised canonical form.

    Returns a NEW dict (never mutates the caller's args). Called once at the
    entry of dispatch_tool so the body synthesis reads ONLY canonical keys.
    Each member increments its TOLERANCE_HITS key when it actually fires.

    Members of the tolerance class (aliased/legacy spellings the schema hides but
    the adapter accepts):
      1. read_document   — `name` tolerated as alias for `document_id`.
      2. edit_table_cell — numeric `col` (positional, un-advertised) coerced to int
        alongside the advertised header-name `column`.
      3. edit_document   — legacy singular old_string/new_string coalesced into the
        advertised edits[] form.
    """
    out = dict(args)
    if name == "read_document":
        if not out.get("document_id"):
            if out.get("name"):
                _tol_hit("read_document.name_alias")
            out["document_id"] = out.get("name") or ""
        out.pop("name", None)
    elif name == "edit_table_cell":
        # Batch: fold a legacy flat single-cell into the advertised edits[] shape. The
        # advertised schema is edits[] (no top-level col); a weak model may still send
        # flat {table_id,row,column,...} — coalesce to a one-element edits list (single
        # cell = list of one). Existing edits[] pass through unchanged. The folded
        # flat keys are DROPPED: the body model is XOR over edits vs the legacy
        # shape, so keeping both fails validation on the very call we folded.
        if out.get("edits") is None:
            if out.get("table_id") is not None:
                _tol_hit("edit_table_cell.flat_single_cell")
            from driver.persistence import _coerce_table_edits

            out["edits"] = _coerce_table_edits(out)
            for k in ("table_id", "row", "column", "col", "old_value", "new_value"):
                out.pop(k, None)
    elif name == "edit_document":
        if out.get("edits") is None:
            _tol_hit("edit_document.legacy_singular")
            out["edits"] = [{
                "old_string": out.get("old_string", ""),
                "new_string": out.get("new_string", ""),
            }]
        # Dropped even when edits[] was supplied: the body model is XOR over
        # edits vs the legacy singular, so a stray legacy key alongside edits[]
        # would fail validation on the canonical call.
        out.pop("old_string", None)
        out.pop("new_string", None)
    return out


def _coerce_search_mode(args: dict) -> str:
    """Coerce search_materials `mode` for the MCP path to "semantic" | "exact".

    NOT a Literal (unlike ToolSearch on the HTTP path): MCP's raw args arrive from a
    third-party model that hallucinates enum spellings (fuzzy/literal/keyword). Turning
    one into a hard error spends the caller's turn on a parameter that was decorative
    until search_materials learned `mode`; coercing to the default keeps the search
    running. The HTTP path uses a Literal instead (ToolSearch) because dsh's own loop
    can self-correct from the 422.
    """
    mode = str(args.get("mode") or "semantic")
    return mode if mode in ("semantic", "exact") else "semantic"


def _search_corpus_arg(args: dict) -> str:
    """Resolve search_materials `corpus` for the MCP path — REJECT, never coerce.

    Deliberately the OPPOSITE of _coerce_search_mode. Why: `mode` is a search-tuning
    knob whose coerced default still runs a real search, but a silently unapplied
    `corpus` returns raw material to a caller who asked for distilled facts (or vice
    versa) — plausible-looking and wrong. So an unknown value is a hard error naming
    every valid one (a third-party model self-corrects in one step), and the legacy
    include_docs/include_refs names are rejected the same way instead of silently
    ignored — pydantic's extra=ignore on the HTTP path is exactly how a dropped switch
    reads as honoured (see reads.py's INVARIANT).
    """
    from agent.search_exec import CORPUS_VALUES

    legacy = [k for k in ("include_docs", "include_refs") if k in args]
    if legacy:
        raise HTTPException(
            status_code=400,
            detail=(
                "search_materials: parameter(s) " + ", ".join(legacy) + " no "
                "longer exist. Use `corpus` with one of: "
                + ", ".join(CORPUS_VALUES) + "."
            ),
        )
    corpus = str(args.get("corpus") or "all")
    if corpus not in CORPUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"search_materials: unknown corpus {corpus!r}. Valid values: "
                + ", ".join(CORPUS_VALUES) + "."
            ),
        )
    return corpus


def _canonical_args(name: str, args: dict) -> dict:
    """Per-tool MCP-side arg conditioning BEFORE body synthesis.

    search_materials is the one tool whose MCP edge carries semantic guards the
    pydantic body cannot express (reject-with-remedy for `corpus`, coerce for
    `mode`, the k clamp, the 500-char query cap, under_document_id
    empty→whole-project): they run here so the HTTP path stays a plain Literal
    and the MCP contract (error codes + messages) is byte-identical to the
    pre-registry dispatcher. Every other tool needs nothing here.
    """
    if name != "search_materials":
        return args
    canonical = dict(args)
    canonical["query"] = str(args.get("query", ""))[:500]
    canonical["corpus"] = _search_corpus_arg(args)
    canonical["k"] = min(20, max(1, int(args.get("k", 5) or 5)))
    canonical["mode"] = _coerce_search_mode(args)
    # `under_document_id` coercion follows the `mode` precedent: missing/empty/
    # whitespace coerces to whole project (a spelling miss from a third-party
    # model must not spend the turn), while a REAL id still reaches the executor
    # — which 404/403s unknown and out-of-scope roots (a permission answer is
    # never coerced away).
    canonical["under_document_id"] = (
        str(args.get("under_document_id") or "").strip() or None
    )
    return canonical


def _require_writable(ctx: dict) -> None:
    """Reject a mutating call on a read-only MCP key.

    # INVARIANT: binary key model — an MCP key is either read-only or read-write, and
    # mutating tools are rejected at dispatch for a read-only key.
    # Why: the subtree scope + the user's
    # explicit act of minting the key IS the trust boundary; every write is
    # reversible via a pre-edit checkpoint in History, so per-write approval on top
    # of the sandbox is redundant. There is no `proposed` state over MCP anymore.
    """
    if ctx.get("auto_apply") is not True:
        raise HTTPException(
            status_code=403,
            detail="This MCP key is read-only; mutating tools are disabled. "
                   "Mint a read-write key to make changes.",
        )


async def dispatch_tool(name: str, args: dict, ctx: dict) -> dict:
    """Route ONE MCP tool call to the registry handler.

    Resolves the name in the ONE registry (404 for anything it does not serve
    on the mcp surface — advertised == routed is structural), applies the
    MCP-side contract (read-only key gate + force-auto for mutating tools by
    effect, arg tolerances, body synthesis for entries that have a request
    model), and calls the SAME in-process handler the Tool-API route calls —
    so telemetry, apply-mode resolution and error mapping are the handler's,
    never a gateway copy.

    # WHY: MCP is always-auto — a read-write MCP key's writes ALWAYS apply
    # directly, never {status:"proposed"}; a system-doc target is rejected with
    # 403 rather than proposed (the agent's own safety config is not editable
    # over MCP). Both live in _resolve_apply_or_force via ctx["force_auto_apply"].
    """
    entry = REGISTRY.get(name)
    if entry is None or "mcp" not in entry.surfaces:
        raise HTTPException(status_code=404, detail=f"Tool not routed over MCP yet: {name}")
    from mcp_gateway.schemas import _served_mcp_names, preview_extractor_visible

    if name not in _served_mcp_names(
        preview_visible=await preview_extractor_visible(),
    ):
        # Deployment gate (preview_extractor) — NOT advertised ⇒ NOT routed, the
        # same single predicate tools/list serves under.
        raise HTTPException(status_code=404, detail=f"Tool not routed over MCP yet: {name}")
    if entry.mutating:
        _require_writable(ctx)
        ctx["force_auto_apply"] = True
    # Normalize AFTER the read-only gate: a malformed tolerated arg (e.g. a
    # non-int `col`) must not raise before the key is rejected as read-only.
    canonical = _canonical_args(name, _normalize_args(name, args))
    handler = resolve_handler(entry)
    if entry.request_model is None:
        # Gateway-only entries: the handler consumes the raw args dict.
        result = await handler(canonical, ctx)
    else:
        body = entry.request_model(**canonical)
        result = await handler(body=body, ctx=ctx)
    await _flag_arrived_reference(result, ctx)
    return result


async def _flag_arrived_reference(result: object, ctx: dict) -> None:
    """Inbox hook: a reference a NON-internal MCP key just created flags its key
    owner.

    # ARCH: keys on the RESULT carrying `reference_id` — the one shape every
    reference-creating handler returns (create_document's reference branch). The
    registry-derived test (tests/backend/test_inbox.py) walks every MCP-served
    tool whose handler touches a reference factory and demands this hook covers
    it, so a tool that starts creating references is caught by CI even if it
    ever returned the id under a different key.

    The HOST is read from the created row (parent_id), never from the call args
    — the args key differs per tool, the row is the one truth. Recipient is the
    key owner (ctx["user_id"]); the dsh driver's internal key never flags.
    """
    if not isinstance(result, dict):
        return
    ref_id = result.get("reference_id")
    if not ref_id or ctx.get("internal") is True:
        return
    row = await fetch_one("documents", extract_id(ref_id))
    if not row or not is_ref_row(row):
        return
    host = row.get("parent_id") or ""
    if not host:
        return
    await mark_arrived(
        KIND_REF, object_id=extract_id(ref_id),
        project_id=row.get("project_id") or ctx["project_id"],
        document_id=str(host), recipient_id=ctx["user_id"],
    )


__all__ = [
    "dispatch_tool",
    "TOLERANCE_HITS",
]

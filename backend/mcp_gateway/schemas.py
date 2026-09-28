"""MCP tool schemas — generated from the agent_tools registry, never re-hardcoded.

# Tool list/parameter assembly. see SYSTEM: mcp-gateway.

# ARCH: the ONE registry
# (agent_tools.registry) is the single source of truth for what the gateway
# ADVERTISES and ROUTES — the mcp-surface entries, converted 1:1 into
# mcp.types.Tool for `tools/list`. A tool's MCP-only description (the clean
# proposal-free wording for mutating tools) is `entry.mcp_description`,
# declared with the tool. This module owns only MCP PRESENTATION: the curated
# emission order, the two advertised-schema projections, the output schemas,
# and the preview_extractor visibility gate.

# INVARIANT: the advertised set IS the routed set. Why: both read the same
# registry mcp entries, so the advertised-vs-routed guard of the pre-registry
# era (two hand-kept tables drifting apart) cannot drift by construction —
# test_mcp_gateway_readonly still pins it, now against the registry.
"""
from __future__ import annotations

from agent_tools.registry import (
    ROOT_SENTINEL,
    fill_properties_root,
    fill_root,
    mcp_entries,
    root_fill,
)
from mcp import types

# INVARIANT(security): every MUTATING tool on the mcp surface whose spec text
# describes the agent/Tool-API proposal flow (the base tools — request_model set)
# MUST carry a registry `mcp_description`. Why: without the override the tool
# silently falls back to the proposal-worded spec text over MCP (the "ignore
# the above" trap this replaced), and the fallback is invisible at runtime —
# asserting at import is what makes a new mutating tool fail loud instead.
# Gateway-only tools (request_model None) are exempt: their spec descriptions
# were authored FOR the MCP surface, so there is no proposal wording to scrub.
_served_mutating = [
    e for e in mcp_entries() if e.mutating and e.request_model is not None
]
assert all(e.mcp_description for e in _served_mutating), (
    "mcp-served mutating tools without a registry mcp_description (would leak "
    "proposal wording): "
    f"{[e.name for e in _served_mutating if not e.mcp_description]}"
)

# Minimal shared result schema for registry-derived tools — the union of the
# id/status fields the mutating + read surfaces emit. No `required` (different
# tools return different subsets) and additionalProperties is allowed so the
# richer read shapes (search results, document content) still validate.
# ARCH: the gateway reuses the SAME executors as the Tool-API / agent path, so this
# is intentionally permissive — it advertises the typed-result contract (capable
# clients read structuredContent) without duplicating every executor's full shape.
# `proposal_id` is deliberately ABSENT: over MCP the binary key model means every
# mutating call force-applies (dispatch_tool sets force_auto_apply), so a
# proposal_id NEVER appears in a gateway result — advertising it would re-teach
# the proposal concept the clean descriptions were scrubbed of.
_AGENT_TOOL_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "doc_id": {"type": "string"},
        "reference_id": {"type": "string"},
        "detail": {"type": "string"},
        "noop": {"type": "boolean"},
    },
}


# Real per-tool output shapes for the READ tools, so a weak model learns the response
# shape from tools/list without a probe call. Mutating tools keep the generic union
# (their result subset varies). Capable clients read structuredContent against these.
_READ_TOOL_OUTPUT_SCHEMAS: dict[str, dict] = {
    "read_document": {
        "type": "object",
        "properties": {
            "doc_id": {"type": "string"},
            "title": {"type": "string"},
            "content": {"type": "string"},
            # Read-path spill bound: the slice
            # window metadata. offset + total_chars ALWAYS present; next_offset
            # only while content remains past the slice (absent ⇒ whole doc).
            "offset": {"type": "integer"},
            "total_chars": {"type": "integer"},
            "next_offset": {"type": "integer"},
            # The file part + referenced-node visibility. has_file disambiguates
            # processing_status=null (an image has has_file=true, status=null).
            "media_type": {"type": ["string", "null"]},
            "has_file": {"type": "boolean"},
            "processing_status": {"type": ["string", "null"]},
            "references": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string"},
                        "title": {"type": "string"},
                        "media_type": {"type": ["string", "null"]},
                        "processing_status": {"type": ["string", "null"]},
                        "has_file": {"type": "boolean"},
                        "created_at": {"type": ["string", "null"]},
                    },
                },
            },
            # read_table folded in. The
            # tables field (index by default, or grids via table_id/inline) is an
            # ADDITIONAL sibling; content still carries raw anchors unchanged.
            "tables": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "table_id": {"type": "string"},
                        "label": {"type": "string"},
                        "n_cols": {"type": "integer"},
                        "rows": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "row": {"type": "integer"},
                                    "cells": {"type": "array", "items": {"type": "string"}},
                                },
                            },
                        },
                    },
                },
            },
        },
    },
    "search_materials": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "hits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "doc_id": {"type": "string"},
                        "parent_id": {"type": ["string", "null"]},
                        "title": {"type": "string"},
                        "snippet": {"type": "string"},
                        # No `score`: hits are ordered by rank fusion, and the array
                        # order is the whole ranking signal (see the INVARIANT on
                        # `_rrf_merge`). `source` is provenance, not relevance.
                        "source": {"type": "string"},
                        # `kind` labels the hit level so a weak model distinguishes a
                        # distilled fact (`memory`) from a raw excerpt (`document` /
                        # `reference`) without a probe call.
                        "kind": {"type": "string"},
                        # `sources` is the pointer to the raw material: the reference
                        # ids a `memory` fact was distilled from. Advertised here so a
                        # model learns from tools/list that it is the route to it
                        # (read_document on a source id returns the full text).
                        # Populated only on `kind:"memory"` hits; empty otherwise.
                        "sources": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "title": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        },
    },
    "get_project_structure": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "documents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string"},
                        "parent_id": {"type": ["string", "null"]},
                        "title": {"type": "string"},
                        "is_index": {"type": "boolean"},
                        "is_reference": {"type": "boolean"},
                        "is_system": {"type": "boolean"},
                        # A memory fact row (plan structure-layered-walk): the
                        # flag must be visible before the root call's hiding of
                        # the Memory subtree can be reasoned about.
                        "is_memory": {"type": "boolean"},
                        # The reference-listing capability (media_type/
                        # source_url, formerly list_references) folded into
                        # this one call. Emitted on REFERENCE rows only;
                        # non-reference rows carry neither key.
                        "media_type": {"type": "string"},
                        "source_url": {"type": ["string", "null"]},
                        # Counters name what the map did NOT deliver below a
                        # row — the drill-down handle. Emitted only when
                        # something is hidden (the row projection carries what
                        # is populated).
                        "child_count": {"type": "integer"},
                        "n_references": {"type": "integer"},
                        "subtree_total": {"type": "integer"},
                        "subtree_depth": {"type": "integer"},
                    },
                },
            },
        },
    },
}


def _entry_to_mcp(entry, fill: str) -> types.Tool:
    """Convert one registry entry to an mcp.types.Tool.

    The spec's `function.parameters` is already a JSON Schema object →
    `inputSchema` directly. A mutating tool serves its clean MCP-only
    `mcp_description` (proposal-free); read tools carry a real per-tool
    outputSchema (_READ_TOOL_OUTPUT_SCHEMAS), everything else the generic
    result union.

    `fill` is the caller's {{ROOT}} fill (registry.root_fill): applied to BOTH
    the tool description and the properties' descriptions — the param-level
    texts are what a model reads when it chooses parent_id:null. Copy-on-need
    (registry.fill_properties_root): a sentinel-free schema keeps the same
    object, byte-identical.
    """
    fn = entry.spec["function"]
    name = entry.name
    description = fill_root(entry.mcp_description or fn.get("description") or "", fill)
    input_schema = fn["parameters"]
    # Two independent projection classes shape what tools/list ADVERTISES; their
    # accept-side twin (what dispatch tolerates) is dispatch._normalize_args. See
    # workflow.md "Name the class on the 2nd fix": a class with >2 members is
    # normalized over the class, not branched per member.
    #
    # Class A — `required`-relax-to-document_id: the mutating tools whose advertised
    # body carries a batch/list the adapter can also accept in a legacy flat shape
    # (edit_document→old_string/new_string; edit_table_cell→table_id/row/column).
    # The MCP SDK validates `required` BEFORE dispatch, so relax it to document_id
    # only so a legacy flat call is not 400'd at validation before it reaches the
    # coalescing path (dispatch._normalize_args).
    if name in ("edit_document", "edit_table_cell"):
        input_schema = {**input_schema, "required": ["document_id"]}
    # {{ROOT}} render: walk INTO the schema (registry.fill_properties_root), not
    # just the tool text — the parent_id descriptions are the shared param-level
    # surface this fill exists for.
    input_schema = fill_properties_root(input_schema, fill)
    # The `mcp_hidden` property-drop machinery is GONE — its only consumer
    # (import_file's sandbox_path/attachment_index) moved off the MCP surface (agent-only
    # now). A tool that never reaches MCP needs no schema projection, and a hidden
    # alias is exactly the wrong-path-that-still-works class this plan deletes.
    return types.Tool(
        name=name,
        description=description,
        inputSchema=input_schema,
        outputSchema=_READ_TOOL_OUTPUT_SCHEMAS.get(name, _AGENT_TOOL_OUTPUT_SCHEMA),
    )


# Hand-written output schemas for the gateway-only tools (mcp-surface entries
# with no Tool-API route). Capable MCP clients read `structuredContent`
# against these; older clients keep reading the text block.
_GATEWAY_OUTPUT_SCHEMAS: dict[str, dict] = {
    "init": {
        "type": "object",
        "properties": {
            "protocol_version": {"type": "integer"},
            "project": {"type": "object"},
            "acting_user": {"type": "object"},
            "instructions": {"type": "string"},
            "rules": {"type": "string"},
            "persona": {"type": ["string", "null"]},
            "skills_index": {"type": "array"},
            "knowledge_index": {"type": "array"},
            "capabilities": {"type": "object"},
        },
    },
    "preview_extractor": {
        "type": "object",
        "properties": {
            "extracted_data": {"type": "object"},
            "rendered_markdown": {"type": "string"},
            "variable_duplicates": {"type": "array", "items": {"type": "string"}},
            "resolved": {
                "type": "object",
                "description": "The resolved inputs the run used (parity evidence).",
                "properties": {
                    "source_doc_id": {"type": "string"},
                    "config_doc_id": {"type": "string"},
                    "target_doc_id": {"type": "string"},
                    "project_id": {"type": "string"},
                    "model": {"type": ["string", "null"]},
                },
            },
        },
    },
    # The mint echoes `attach_to` (the host), NOT `document_id` — the node id
    # is born at redeem, so the same key must not name two different nodes across
    # the two halves of one operation. expires_in_s is relative (no clock
    # comparison); poll_after_s bounds the wait (coarse, not a promise).
    "attach_file": {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "method": {"type": "string"},
            "field_name": {"type": "string"},
            "filename": {"type": "string"},
            "attach_to": {"type": "string"},
            "accepted": {"type": "boolean"},
            "max_size_mb": {"type": "integer"},
            "expires_in_s": {"type": "integer"},
            "expires_at": {"type": "string"},
            "poll_after_s": {"type": "integer"},
            "example": {"type": "string"},
        },
    },
    "get_file": {
        "type": "object",
        "properties": {
            "document_id": {"type": "string"},
            "url": {"type": "string"},
            "filename": {"type": "string"},
            "expires_at": {"type": "string"},
        },
    },
    "reprocess_file": {
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "document_id": {"type": "string"},
        },
    },
}


# Advertised tools/list order, curated for a weak model reading top-to-bottom:
# init → reads → text → file → tree → tables → gated extractor. Any advertised
# tool not named here is appended after (guarded: a missing/extra name is caught
# by the assembly below). Grouping follows the PART-of-a-node decomposition:
# text part tools, then file part tools, then tree part tools.
_TOOL_LIST_ORDER: tuple[str, ...] = (
    "init",
    # reads (incl. read_document — addresses the text part but grouped as a read)
    "search_materials", "read_document", "get_project_structure",
    # text part
    "create_document", "edit_document", "append_to_document",
    # file part — the ONE byte channel (attach_file mints the URL; no bytes as args)
    "attach_file", "get_file", "reprocess_file",
    # tree part
    "move_document",
    # tables
    "create_table", "edit_table_cell", "add_table_rows", "add_table_column",
    # dev/benchmark — heavy, dry-run only, advertised last (gated off by default)
    "preview_extractor",
)


def _preview_extractor_visible() -> bool:
    """preview_extractor is gated off the default MCP surface (a ~50s
    dev/benchmark tool advertised to every external client is a footgun).

    Sync env⊕default leg (settings.bootstrap_value) — the ONLY readers left on
    it are import-time seams (the module's render asserts): no loop, no DB there,
    and a live override lands on the request-time legs instead
    (preview_extractor_visible below)."""
    import settings

    return bool(settings.bootstrap_value("MCP_RUN_EXTRACTOR"))


async def preview_extractor_visible() -> bool:
    """Live leg (row → env → default) of the preview_extractor gate for
    request-time surface builds (tools/list, dispatch, the init bootstrap) —
    a settings PUT reaches the very next request."""
    import settings

    return bool(await settings.get("MCP_RUN_EXTRACTOR"))


def _served_mcp_names(preview_visible: bool | None = None) -> set[str]:
    """The registry mcp-surface names actually SERVED on this deployment —
    preview_extractor dropped unless MCP_RUN_EXTRACTOR is set. The single
    source for build_tool_list (what tools/list advertises); dispatch_tool
    routes the ungated table, and a gated-off preview_extractor call answers
    404 from the same predicate the gateway serves it under (the tool is
    unreachable either way — the gate is deployment configuration, not a
    per-call secret).

    `preview_visible=None` resolves the sync env leg (import-time callers);
    request-time callers pass the awaited live leg."""
    names = {e.name for e in mcp_entries()}
    if preview_visible is None:
        preview_visible = _preview_extractor_visible()
    if not preview_visible:
        names.discard("preview_extractor")
    return names


def build_tool_list(*, scope_root: str = "", root_title: str = "",
                    preview_visible: bool | None = None) -> list[types.Tool]:
    """The full MCP tool surface: the registry's mcp entries, emitted in the
    curated _TOOL_LIST_ORDER (init → reads → text → file → tree → tables) so a
    weak model reads the surface in a logical sequence. Any tool not listed
    there is appended (defensive — keeps a newly added tool visible even before
    it is ordered).

    preview_extractor is dropped from the advertised surface unless
    MCP_RUN_EXTRACTOR is set (a ~50s dev/benchmark tool has no place on every
    external client's tools/list by default). Request-time callers pass the
    awaited `preview_extractor_visible()`; None (the default) resolves the
    sync env leg and is only for import-time seams.

    # ARCH ({{ROOT}} render): the surface is RENDERED for the caller's root —
    # `scope_root`/`root_title` (registry.root_fill fills the sentinel texts with
    # the caller's own root). No args = the unscoped constant, which is TRUE for
    # an unscoped key. server._list_tools authenticates the Bearer and passes the
    # key's real scope; a caller that renders per key must NEVER fall back to
    # this unscoped form on auth failure (the wording is the lie that path
    # exists to remove).
    """
    fill = root_fill(scope_root, root_title)
    by_name = {e.name: e for e in mcp_entries()}
    served = _served_mcp_names(preview_visible)

    def _build(name: str) -> types.Tool:
        entry = by_name[name]
        output = _GATEWAY_OUTPUT_SCHEMAS.get(name)
        if output is not None and entry.request_model is None:
            return types.Tool(
                name=name,
                description=fill_root(
                    entry.spec["function"]["description"], fill,
                ),
                inputSchema=fill_properties_root(
                    entry.spec["function"]["parameters"], fill,
                ),
                outputSchema=output,
            )
        return _entry_to_mcp(entry, fill)

    ordered = list(_TOOL_LIST_ORDER)
    known = set(by_name) & served
    # preview_extractor is in _TOOL_LIST_ORDER but gated out of `served`
    # above; the `n in known` filter drops it from the ordered emission when off.
    leftover = [n for n in by_name if n not in ordered and n in known]
    return [_build(n) for n in ordered if n in known] + [_build(n) for n in leftover]


# The served surface is RENDERED ({{ROOT}} filled per caller), so the import-time
# description guarantees must run over the OUTPUT, not the template: render BOTH
# kinds — the unscoped call and a fake scoped pair (DB-free; server._list_tools
# does the real title fetch) — and refuse, at EITHER kind and BOTH levels
# (Tool.description AND inputSchema.properties.*.description), an empty
# description or an unfilled sentinel. The pre-render asserts guaranteed the
# static strings; what ships is the render, and a sentinel some fill path forgot
# to walk is exactly the invisible-at-runtime failure this catches at import.
#
# WHY the sentinel sweep goes DEEPER than the fill: registry.fill_properties_root
# walks the schema's top-level properties, which is where every sentinel site
# lives today. An assert scoped to the same level could only ever confirm the
# fill's own reach — a sentinel written into a nested `items`/`$defs`/anyOf
# member would ship unfilled with both of them green. So the emptiness check
# stays on the two SERVED text levels, and the sentinel check walks the whole
# rendered schema: the assert fails on the day a site lands where the fill does
# not reach, which is the only way it can catch that class at all.
def _walk_schema_strings(node, path: str):
    """(path, str) for every string anywhere in a rendered schema."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_schema_strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_schema_strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def _rendered_surface_defects(scope_root: str, root_title: str) -> list[str]:
    defects: list[str] = []
    for tool in build_tool_list(scope_root=scope_root, root_title=root_title):
        texts = [(tool.name, tool.description or "")]
        for pname, p in (tool.input_schema.get("properties") or {}).items():
            if isinstance(p, dict) and p.get("description"):
                texts.append((f"{tool.name}.{pname}", p["description"]))
        for which, text in texts:
            if not text.strip():
                defects.append(f"{which}: empty description")
        for which, text in [
            *texts,
            *_walk_schema_strings(tool.input_schema, f"{tool.name}.inputSchema"),
            *_walk_schema_strings(tool.output_schema, f"{tool.name}.outputSchema"),
        ]:
            if ROOT_SENTINEL in text:
                defects.append(f"{which}: unfilled {ROOT_SENTINEL}")
    return defects


assert not _rendered_surface_defects("", ""), (
    "unscoped render defects: " + ", ".join(_rendered_surface_defects("", ""))
)
assert not _rendered_surface_defects("surface-assert-root", "Surface Assert Root"), (
    "scoped render defects: "
    + ", ".join(
        _rendered_surface_defects("surface-assert-root", "Surface Assert Root")
    )
)


__all__ = ["build_tool_list"]

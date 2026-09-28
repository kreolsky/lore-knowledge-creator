"""The ONE declaration per agent tool.

Each `register(...)` ties a tool's FIVE aspects together — wire spec (from
agent_tools.specs), request model (models), handler (dotted path, resolved
lazily), surfaces, and the optional agent env gate — and nothing else in Python
needs to repeat them: the agent toolset (AGENT_TOOLS / agent_toolset /
MUTATING_TOOLS via agent.tools), the Tool-API POST routes
(agent_tools.routing), and the MCP surface all derive from this table.

DECLARATION ORDER IS THE SERVING ORDER: agent_toolset() filters it, never
re-sorts, and the "mcp" subset of it is AGENT_TOOLS. Order: the public base
surface (reads → mutates — the tools every surface serves), then the agent-only
tools (gated first, then always-served).

The base entries iterate the spec LISTS (READ_TOOLS + MUTATE_TOOLS) so the
declaration never drifts from the spec constants' own order.
"""
from agent_tools.registry import ToolEntry, register
from agent_tools.specs.media import (
    GENERATE_IMAGE_TOOL,
    IMPORT_FILE_TOOL,
    REPROCESS_REFERENCE_TOOL,
    SANDBOX_FETCH_REFERENCE_TOOL,
    SANDBOX_FETCH_SKILL_TOOL,
    SANDBOX_RUN_STATUS_TOOL,
    SANDBOX_TOOL,
)
from agent_tools.specs.memory import (
    APPLY_MEMORY_VERDICTS_TOOL,
    CONSOLIDATE_MEMORY_TOOL,
    NEXT_REFERENCE_TOOL,
    REOPEN_CONSOLIDATION_TOOL,
)
from agent_tools.specs.mutate import MUTATE_TOOLS
from agent_tools.specs.read import (
    GET_FACT_HISTORY_TOOL,
    GET_MEMORY_FACTS_TOOL,
    READ_TOOLS,
)
from agent_tools.specs.skills import SAVE_SKILL_TOOL
from models import (
    ToolAddTableColumn,
    ToolAddTableRows,
    ToolAppendDocument,
    ToolApplyMemoryVerdicts,
    ToolConsolidateMemory,
    ToolCreateDocument,
    ToolCreateTable,
    ToolEditDocument,
    ToolEditTableCell,
    ToolGenerateImage,
    ToolGetFactHistory,
    ToolGetMemoryFacts,
    ToolGetProjectStructure,
    ToolImportFile,
    ToolMoveDocument,
    ToolNextReference,
    ToolReadDocument,
    ToolRenameDocument,
    ToolReopenConsolidation,
    ToolReprocessReference,
    ToolSandboxBash,
    ToolSandboxFetchReference,
    ToolSandboxFetchSkill,
    ToolSandboxRunStatus,
    ToolSaveSkill,
    ToolSearch,
)

# ─── The public base surface (pi + tool_api + mcp) ────────────────────────────
# (model, handler) per base tool, in READ_TOOLS + MUTATE_TOOLS order.
_BASE: tuple[tuple[type, str], ...] = (
    # READ_TOOLS: search_materials, read_document, get_project_structure
    (ToolSearch, "routes.tool_api.reads:tool_search_materials"),
    (ToolReadDocument, "routes.tool_api.reads:tool_read_document"),
    (ToolGetProjectStructure, "routes.tool_api.reads:tool_get_project_structure"),
    # MUTATE_TOOLS: create_document .. rename_document
    (ToolCreateDocument, "routes.tool_api.creates:tool_create_document"),
    (ToolEditDocument, "routes.tool_api.edits:tool_edit_document"),
    (ToolEditTableCell, "routes.tool_api.edits:tool_edit_table_cell"),
    (ToolCreateTable, "routes.tool_api.edits:tool_create_table"),
    (ToolAddTableRows, "routes.tool_api.edits:tool_add_table_rows"),
    (ToolAddTableColumn, "routes.tool_api.edits:tool_add_table_column"),
    (ToolAppendDocument, "routes.tool_api.edits:tool_append_document"),
    (ToolMoveDocument, "routes.tool_api.structure:tool_move_document"),
    (ToolRenameDocument, "routes.tool_api.structure:tool_rename_document"),
)

# MCP-only descriptions for the mutating base tools (entry.mcp_description).
# The shared spec descriptions describe the in-app Tool-API proposal/confirmation
# flow (real there — resolve_apply_mode / status:"proposed" in routes.tool_api).
# That flow does NOT exist over MCP, so the MCP surface REPLACES the description
# with clean, proposal-free wording: direct-apply, reversible in History,
# read-only key → 403. schemas asserts every mcp-served mutating entry has one.
_MCP_MUTATING_DESCRIPTIONS: dict[str, str] = {
    "edit_document": (
        "Apply pointwise str_replace edits as one atomic batch. Each old_string must "
        "be a small verbatim substring from read_document. To place an existing image "
        "in the text, write `![alt|800x600](ref:<document_id>)` using an id from "
        "`references[]` or `attach_file`'s POST response — never embed file bytes or "
        "base64 in text (files live on nodes, not in the buffer). Remote image URLs "
        "are not supported inline; attach the file and reference it by id. Read-write "
        "key: applies immediately, reversible in the Lore History panel."
    ),
    "create_document": (
        "Create a new document or reference. `parent_id` places the node: a tree "
        "child document for `node_type`=\"document\" (the default; null = "
        "{{ROOT}}), or the HOST of a `node_type`=\"reference\" leaf (visible "
        "from it and its descendants, cannot itself have children). For a leaf "
        "whose content comes from a file, use `attach_file`. To place an "
        "existing image in the text, write `![alt|800x600](ref:<document_id>)` using an "
        "id from `references[]` or `attach_file`'s POST response — never embed file "
        "bytes or base64 in text (files live on nodes, not in the buffer). Remote "
        "image URLs are not supported inline; attach the file and reference it by id. "
        "Read-write key: applies immediately, reversible in the Lore History panel."
    ),
    "edit_table_cell": (
        "Edit one or more table cells as one atomic batch (edits: "
        "[{table_id, row, column, old_value, new_value}]). Address each cell by "
        "(row, column-name); old_value must be verbatim from read_document. Read-write "
        "key: applies immediately, reversible in the Lore History panel."
    ),
    "create_table": (
        "Append a NEW editable table block (header + initial rows) at the document tail, "
        "or at the end of one heading section (pass the heading text as `section`). "
        "rows[0] is the header. Returns the new table_id. Read-write key: applies "
        "immediately, reversible in the Lore History panel."
    ),
    "add_table_rows": (
        "Append N rows to the bottom of an existing table. rows is a positional matrix "
        "of cell values (rows[i] = the cells of row i); shorter rows pad empty, an "
        "over-length row is rejected. Call read_document first for the live width. "
        "Read-write key: applies immediately, reversible in the Lore History panel."
    ),
    "add_table_column": (
        "Insert one column into an existing table. header = the row-0 cell of the new "
        "column; values = the data-row cells (padded empty); at_index = insert position "
        "(default = the end). Every existing row gets a new cell. Read-write key: "
        "applies immediately, reversible in the Lore History panel."
    ),
    "append_to_document": (
        "Append markdown at the end of a document, or at the end of one heading "
        "section (pass the heading text as `section`). Prefer this over edit_document "
        "when only adding content at a tail. To place an existing image in the text, "
        "write `![alt|800x600](ref:<document_id>)` using an id from `references[]` or "
        "`attach_file`'s POST response — never embed file bytes or base64 in text "
        "(files live on nodes, not in the buffer). Remote image URLs are not "
        "supported inline; attach the file and reference it by id. Read-write key: "
        "applies immediately, reversible in the Lore History panel."
    ),
    "move_document": (
        "Move any node under a different parent — a tree document or an attached "
        "file node, same call. An attached node is visible from its host document "
        "and all of that host's descendants, so hosting it on the project's index "
        "document makes it visible project-wide. parent_id = new parent (null = "
        "{{ROOT}}); after_id = a sibling to land after under the target parent "
        "(null = top). after_id orders tree documents only; attached nodes are not "
        "tree-ordered, so passing it for one is an error. Omit `node_type` to KEEP "
        "the node's kind; pass node_type=\"reference\" to convert a document into a "
        "reference of parent_id (the host) — after_id and a root parent are "
        "rejected, and a document with children is refused; pass "
        "node_type=\"document\" to convert a reference into a tree document — a "
        "file-backed reference (audio/image bytes) is refused. NOT reversible via the "
        "Lore History panel (move is structural) — undo by moving it back. "
        "Read-write key: applies immediately."
    ),
    "rename_document": (
        "Rename any node — set a new title. A tree document or an attached file "
        "node, same call. `title` is stripped and must be non-empty. Titles are "
        "not unique and ids are permanent: a rename never changes `document_id` "
        "and never breaks `ref:<id>` / `table:<id>` anchors or mention links. "
        "NOT reversible via the Lore History panel (rename is structural, not a "
        "content edit) — undo by renaming back. Read-write key: applies "
        "immediately."
    ),
}

_PUBLIC = frozenset({"agent", "tool_api", "mcp"})
_AGENT_HTTP = frozenset({"agent", "tool_api"})

for _spec, (_model, _handler) in zip((*READ_TOOLS, *MUTATE_TOOLS), _BASE):
    register(ToolEntry(
        spec=_spec, request_model=_model, handler_path=_handler,
        surfaces=_PUBLIC,
        mcp_description=_MCP_MUTATING_DESCRIPTIONS.get(_spec["function"]["name"]),
    ))

# ─── Agent-only tools (tool_api route, never advertised over MCP) ────────────────
# Posture notes (why each is NOT on the public MCP surface) live at each tool's
# spec definition site in agent_tools/specs — this table only records the facts.

# Env-gated (an external MCP client cannot reach the sandbox host / ComfyUI
# internal services; unset config = not served on the agent path either —
# a valid deploy, not an error).
register(ToolEntry(
    spec=SANDBOX_TOOL, request_model=ToolSandboxBash,
    handler_path="routes.tool_api.sandbox.bash:tool_sandbox_bash",
    surfaces=_AGENT_HTTP, env_gate="sandbox",
))
register(ToolEntry(
    spec=SANDBOX_FETCH_REFERENCE_TOOL, request_model=ToolSandboxFetchReference,
    handler_path="routes.tool_api.sandbox.bash:tool_sandbox_fetch_reference",
    surfaces=_AGENT_HTTP, env_gate="sandbox",
))
register(ToolEntry(
    spec=SANDBOX_FETCH_SKILL_TOOL, request_model=ToolSandboxFetchSkill,
    handler_path="routes.tool_api.sandbox.bash:tool_sandbox_fetch_skill",
    surfaces=_AGENT_HTTP, env_gate="sandbox",
))
register(ToolEntry(
    spec=SANDBOX_RUN_STATUS_TOOL, request_model=ToolSandboxRunStatus,
    handler_path="routes.tool_api.sandbox.runs:tool_sandbox_run_status",
    surfaces=_AGENT_HTTP, env_gate="sandbox",
))
register(ToolEntry(
    spec=GENERATE_IMAGE_TOOL, request_model=ToolGenerateImage,
    handler_path="routes.tool_api.image_gen.tool:tool_generate_image",
    surfaces=_AGENT_HTTP, env_gate="comfyui",
))

# Always served on the agent path.
register(ToolEntry(
    spec=REPROCESS_REFERENCE_TOOL, request_model=ToolReprocessReference,
    handler_path="routes.tool_api.imports:tool_reprocess_reference",
    surfaces=_AGENT_HTTP,
))
register(ToolEntry(
    spec=IMPORT_FILE_TOOL, request_model=ToolImportFile,
    handler_path="routes.tool_api.imports:tool_import_file",
    surfaces=_AGENT_HTTP,
))
register(ToolEntry(
    spec=CONSOLIDATE_MEMORY_TOOL, request_model=ToolConsolidateMemory,
    handler_path="routes.tool_api.memory:tool_consolidate_memory",
    surfaces=_AGENT_HTTP,
))
register(ToolEntry(
    spec=NEXT_REFERENCE_TOOL, request_model=ToolNextReference,
    handler_path="routes.tool_api.memory:tool_next_reference",
    surfaces=_AGENT_HTTP,
))
register(ToolEntry(
    spec=GET_MEMORY_FACTS_TOOL, request_model=ToolGetMemoryFacts,
    handler_path="routes.tool_api.memory:tool_get_memory_facts",
    surfaces=_AGENT_HTTP,
))
register(ToolEntry(
    spec=GET_FACT_HISTORY_TOOL, request_model=ToolGetFactHistory,
    handler_path="routes.tool_api.memory:tool_get_fact_history",
    surfaces=_AGENT_HTTP,
))
register(ToolEntry(
    spec=APPLY_MEMORY_VERDICTS_TOOL, request_model=ToolApplyMemoryVerdicts,
    handler_path="routes.tool_api.memory:tool_apply_memory_verdicts",
    surfaces=_AGENT_HTTP,
))
register(ToolEntry(
    spec=REOPEN_CONSOLIDATION_TOOL, request_model=ToolReopenConsolidation,
    handler_path="routes.tool_api.memory:tool_reopen_consolidation",
    surfaces=_AGENT_HTTP,
))
# save_skill (plan skill-authoring-skill): agent-only + always served (no env
# gate — no external dependency), CORE on purpose so a model that skipped the
# skill-authoring skill still finds it by name (see specs/skills.py ARCH).
register(ToolEntry(
    spec=SAVE_SKILL_TOOL, request_model=ToolSaveSkill,
    handler_path="routes.tool_api.skills:tool_save_skill",
    surfaces=_AGENT_HTTP,
))

"""Mutating agent tool CONSTANTS: the document/table write entries of
AGENT_TOOLS (MUTATE_TOOLS) — create/edit/append/move_document/rename_document
+ the table tools + the subtree zip import. Pure data — imports nothing from
the agent package (leaf; the subtree spec dict comes from the sibling
specs/subtree.py data module).

MOVE_CHAT_TOOL stands beside that list, not in it: it is the `structure`
skill's pack and never reaches MCP (see the constant).

The create_document html sentence is RENDERED from config + the artifact
library manifest (artifacts.html_node_sentence) so an install
without artifact hosting never advertises node_type="html" in prose.
"""
from artifacts import html_node_sentence

from agent_tools.specs.subtree import IMPORT_SUBTREE_MD_TOOL

MUTATE_TOOLS: list[dict] = [
    {
        "type": "function",
        "mutating": True,
        "function": {
            "name": "create_document",
            "description": (
                "Create a document or reference. `parent_id` places it: the tree "
                "parent of a document (null = {{ROOT}}; the project's index "
                "document as a document's parent means the project root, so the result "
                "shows parent_id null), or the HOST of a reference "
                "leaf (visible from the host and its descendants; no children). "
                "Content from a file: `import_file`."
                + html_node_sentence()
                + " In confirmation mode the call is held for the user's approval; it applies on approval, or returns `rejected` with the user's feedback if declined."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string", "description": "Full markdown body (or the whole HTML file for node_type=\"html\")."},
                    "parent_id": {
                        "type": ["string", "null"],
                        "description": (
                            "Required. Tree parent for a document (null = {{ROOT}}); "
                            "the host for a reference (never null)."
                        ),
                    },
                    "node_type": {
                        "type": "string",
                        "enum": ["document", "reference", "html"],
                        "default": "document",
                        "description": (
                            "`document` (default); `reference` = a leaf on host "
                            "parent_id; `html` = an interactive page leaf, one "
                            "self-contained HTML file (only with artifact hosting). "
                            "Audio/image nodes come only from import_file."
                        ),
                    },
                    # Normalize carries the .md
                    # normalize capability that left the upload surface with `content`
                    # Default False — agent-authored markdown must NOT be
                    # reflowed (it would rewrite text the user is about to review in a
                    # proposal card). Callers importing a .md file's text pass True.
                    "normalize": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Reflow wrapped prose and dedent list-nested fences in "
                            "`content`. Never for text you authored; true only for an "
                            "imported .md file's text with hard line breaks."
                        ),
                    },
                },
                "required": ["title", "content", "parent_id"],
            },
        },
    },
    {
        "type": "function",
        "mutating": True,
        "function": {
            "name": "edit_document",
            "description": (
                "Propose pointwise str_replace edits as one atomic batch (edits: "
                "[{old_string, new_string}]). In confirmation mode the call is held for the user's approval; it applies on approval, or returns `rejected` with the user's feedback if declined. Reproduce hex code "
                "spans (`#rrggbb text` — colored highlights) and ![…](table:…) anchors "
                "byte-for-byte inside old_string and new_string; an inexact copy "
                "destroys them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "description": (
                            "Ordered list of pointwise edits to apply atomically. Each "
                            "edit targets an independent, non-overlapping region."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_string": {
                                    "type": "string",
                                    "description": "Verbatim text to replace, copied from read_document.",
                                },
                                "new_string": {
                                    "type": "string",
                                    "description": "Replacement text.",
                                },
                            },
                            "required": ["old_string", "new_string"],
                        },
                    },
                },
                "required": ["document_id", "edits"],
            },
        },
    },
    {
        "type": "function",
        "mutating": True,
        "function": {
            "name": "edit_table_cell",
            "description": (
                "Propose editing one or more table cells as one atomic batch (edits: "
                "[{table_id, row, column, old_value, new_value}]). Address each cell by "
                "(row, column-name); a single cell is a list of one. In confirmation mode the call is held for the user's approval; it applies on approval, or returns `rejected` with the user's feedback if declined."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "edits": {
                        "type": "array",
                        "description": (
                            "Ordered list of cell edits to apply atomically. Each edit "
                            "targets an independent cell."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "table_id": {"type": "string"},
                                "row": {"type": "integer", "minimum": 0},
                                "column": {
                                    "type": "string",
                                    "description": (
                                        "Header NAME of the target column (from "
                                        "read_document's tables index/header)."
                                    ),
                                },
                                "old_value": {
                                    "type": "string",
                                    "description": "Verbatim current cell text (from read_document).",
                                },
                                "new_value": {"type": "string"},
                            },
                            # WHY: `column` is REQUIRED per edit — col/column both
                            # optional let weak models send neither and apply 500'd with
                            # no coordinate to resolve (edit-table-cell-column-apply-fix).  Why: making `column` required (not just col/column optional) stops a weak model from sending neither and 500'ing the apply with no coordinate to resolve.
                            # The numeric `col` is a dispatch-only fallback, NOT advertised.
                            "required": [
                                "table_id", "row", "column", "old_value", "new_value",
                            ],
                        },
                    },
                },
                "required": ["document_id", "edits"],
            },
        },
    },
    {
        "type": "function",
        "mutating": True,
        "function": {
            "name": "create_table",
            "description": (
                "Propose creating a NEW editable table block (header + initial rows) "
                "appended at the document tail, or at the end of one heading section "
                "(pass the heading text as `section`). rows[0] is the header. In "
                "confirmation mode the call is held for the user's approval; it "
                "applies on approval, or returns `rejected`. Always call "
                "read_document first so the section name is exact. Reversible via the "
                "Lore History panel. A pipe table you write into a document body "
                "becomes a table object on its own lines; use create_table to NAME a "
                "table (label) or to place it at the end of a specific section."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "label": {
                        "type": "string",
                        "description": "Anchor label for the table (default 'Table').",
                    },
                    "rows": {
                        "type": "array",
                        "items": {"type": "array", "items": {"type": "string"}},
                        "description": "Positional cell matrix; rows[0] is the header.",
                    },
                    "section": {
                        "type": "string",
                        "description": (
                            "Optional heading text — append the table at the end of that "
                            "section instead of the whole document."
                        ),
                    },
                },
                "required": ["document_id", "rows"],
            },
        },
    },
    {
        "type": "function",
        "mutating": True,
        "function": {
            "name": "add_table_rows",
            "description": (
                "Propose appending N rows to the bottom of an existing table. rows is a "
                "positional matrix of cell values (rows[i] = the cells of row i); shorter "
                "rows pad empty, an over-length row is rejected (400). Always call "
                "read_document first for the live width. Reversible via the Lore History "
                "panel. In confirmation mode the call is held for the user's approval; it applies on approval, or returns `rejected` with the user's feedback if declined."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "table_id": {"type": "string"},
                    "rows": {
                        "type": "array",
                        "items": {"type": "array", "items": {"type": "string"}},
                        "description": "Positional cell matrix; rows[i] = the cells of row i.",
                    },
                },
                "required": ["document_id", "table_id", "rows"],
            },
        },
    },
    {
        "type": "function",
        "mutating": True,
        "function": {
            "name": "add_table_column",
            "description": (
                "Propose inserting ONE column into an existing table. header = the row-0 "
                "cell of the new column; values = the data-row cells (padded empty); "
                "at_index = insert position (default = the end). Every existing row gets a "
                "new cell at at_index. Always call read_document first for the live column "
                "count. Reversible via the Lore History panel. In confirmation mode the call is held for the user's approval; it applies on approval, or returns `rejected` with the user's feedback if declined."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "table_id": {"type": "string"},
                    "header": {
                        "type": "string",
                        "description": "The new column's header cell (row 0).",
                    },
                    "values": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Data-row cells of the new column (padded empty).",
                    },
                    "at_index": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Insert position; default = the end (len(columns)).",
                    },
                },
                "required": ["document_id", "table_id"],
            },
        },
    },
    {
        "type": "function",
        "mutating": True,
        "function": {
            "name": "append_to_document",
            "description": (
                "Propose appending markdown at the end of a document, or at the end "
                "of one heading section (pass the heading text as `section`). Use this "
                "instead of edit_document when you only need to ADD content at a tail. "
                "In confirmation mode the call is held for the user's approval; it applies on approval, or returns `rejected` with the user's feedback if declined."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "content": {
                        "type": "string",
                        "description": "Markdown to append.",
                    },
                    "section": {
                        "type": "string",
                        "description": (
                            "Optional heading text — append at the end of that "
                            "section instead of the whole document."
                        ),
                    },
                },
                "required": ["document_id", "content"],
            },
        },
    },
    {
        "type": "function",
        "mutating": True,
        "function": {
            "name": "move_document",
            "description": (
                "Propose moving a node (tree document or reference) under a new parent "
                "and/or position: parent_id = new parent (null = {{ROOT}}; for a tree "
                "document the project's index document means the project root, so the "
                "result shows parent_id null), after_id = "
                "the sibling to land after (null = top; tree documents only). To "
                "reorder in place, pass the current parent_id. A reference is visible "
                "from its host and the host's descendants — host it on the index "
                "document for project-wide reach. `node_type` converts in the same op: "
                "`reference` makes a document a reference of host parent_id (no "
                "after_id, no root, refused if it has children); `document` makes a "
                "reference a tree document (refused for a file-backed one). Not "
                "undoable in the History panel — undo by moving back. "
                "In confirmation mode the call is held for the user's approval; it applies on approval, or returns `rejected` with the user's feedback if declined."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "parent_id": {
                        "type": ["string", "null"],
                        "description": (
                            "New parent id; null or omitted = {{ROOT}} (not for a "
                            "reference — host it on the index document). The HOST "
                            "for node_type `reference` (required)."
                        ),
                    },
                    "after_id": {
                        "type": "string",
                        "description": "Sibling to place after; null = top.",
                    },
                    "node_type": {
                        "type": "string",
                        "enum": ["document", "reference"],
                        "description": (
                            "Omit to keep the kind; a value converts (see above). "
                            "The kind it already is = a plain move."
                        ),
                    },
                },
                "required": ["document_id"],
            },
        },
    },
    {
        "type": "function",
        "mutating": True,
        "function": {
            "name": "rename_document",
            "description": (
                "Propose renaming a node — set a new title. Rename any node, a "
                "tree document or an attached file node, same call. `title` is "
                "stripped and must be non-empty. Titles are not unique and ids "
                "are permanent: a rename never changes `document_id` and never "
                "breaks `ref:<id>` / `table:<id>` anchors or mention links. NOT "
                "reversible via the Lore History panel (rename is structural, "
                "not a content edit) — undo by renaming back. In confirmation "
                "mode the call is held for the user's approval; it applies on "
                "approval, or returns `rejected` with the user's feedback if "
                "declined."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "title": {
                        "type": "string",
                        "description": (
                            "The new title. Titles need not be unique — ids are "
                            "the address."
                        ),
                    },
                },
                "required": ["document_id", "title"],
            },
        },
    },
    IMPORT_SUBTREE_MD_TOOL,
    {
        "type": "function",
        "mutating": True,
        "function": {
            "name": "restore_document_version",
            "description": (
                "Restore a document to a saved version from list_document_versions. "
                "First call diff_document_versions(from_checkpoint_id=<it>, "
                "to=\"current\") and tell the user what will change. The restore "
                "replaces the whole document for every co-editor. The state before the "
                "restore is saved and returned as `backup_checkpoint_id` — to undo, "
                "restore that. In confirmation mode the call is held for the user's "
                "approval; it applies on approval, or returns `rejected` with the "
                "user's feedback if declined."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "checkpoint_id": {"type": "string"},
                },
                "required": ["document_id", "checkpoint_id"],
            },
        },
    },
]

# ARCH: move_chat is agent-only (agent + tool_api, never MCP) and NOT a member
# of MUTATE_TOOLS: that list is the public base surface, registered with the
# MCP surface included. A chat is ONE PERSON's own row — an MCP key has no
# person whose chats these are (the chat-recall posture, specs/chat_recall.py).
# It is packed by the shipped `structure` skill (backend/configs/
# skill_structure.md): a packed tool is neither listed nor loadable until that
# skill is enabled.
MOVE_CHAT_TOOL: dict = {
    "type": "function",
    "mutating": True,
    "function": {
        "name": "move_chat",
        "description": (
            "Move a chat under another document, or back to project level. "
            "`document_id` is the destination: a document of this project (take "
            "ids from `get_project_structure`), or null for project level — "
            "never the project's index document, which anchors the chat ON it; "
            "a reference is refused. Omit `chat_id` to move THIS chat, and do "
            "it as the LAST step of your turn: later document writes are "
            "checked against the new parent. To move another of the user's "
            "chats, pass its `chat_id` from `list_my_chats`. In confirmation "
            "mode the call is held for the user's approval; it applies on "
            "approval, or returns `rejected` with the user's feedback if "
            "declined."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "string",
                    "description": (
                        "The chat to move, its id from `list_my_chats`; omit "
                        "to move THIS chat."
                    ),
                },
                "document_id": {
                    "type": ["string", "null"],
                    "description": "Destination document id, or null for project level.",
                },
            },
            "required": ["document_id"],
        },
    },
}


# ARCH: set_chat_persona is agent-only like move_chat (see the ARCH above it) and
# packed by the same `structure` skill — a chat's persona is its binding, not a
# document write.
SET_CHAT_PERSONA_TOOL: dict = {
    "type": "function",
    "mutating": True,
    "function": {
        "name": "set_chat_persona",
        "description": (
            "Switch the persona a chat of the user runs under — the same choice as "
            "the persona picker, including the chat memory it belongs to. "
            "`persona_id` is a persona document of this project (the direct "
            "children of the Personas folder in `get_project_structure`), or null "
            "for Default. Omit `chat_id` for THIS chat: the new persona takes effect "
            "from the next turn, so make it the LAST step of your turn. To change "
            "another of the user's chats, pass its `chat_id` from `list_my_chats`. "
            "Only the user's own chats; notes are refused. In confirmation mode the "
            "call is held for the user's approval; it applies on approval, or "
            "returns `rejected` with the user's feedback if declined."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "string",
                    "description": (
                        "The chat to change, its id from `list_my_chats`; omit "
                        "for THIS chat."
                    ),
                },
                "persona_id": {
                    "type": ["string", "null"],
                    "description": "Persona document id, or null for Default.",
                },
            },
            "required": ["persona_id"],
        },
    },
}

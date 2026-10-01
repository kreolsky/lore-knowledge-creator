"""Mutating agent tool CONSTANTS: the document/table write entries of
AGENT_TOOLS (MUTATE_TOOLS) — create/edit/append/move_document/rename_document
+ the table tools. Pure data — imports nothing from the agent package (leaf).
"""

MUTATE_TOOLS: list[dict] = [
    {
        "type": "function",
        "mutating": True,
        "function": {
            "name": "create_document",
            "description": (
                "Create a new document or reference. `parent_id` places the node: "
                "a tree child document for `node_type`=\"document\" (the default; "
                "null = the project root), or the HOST of a `node_type`="
                "\"reference\" leaf (visible from it and its descendants, cannot "
                "itself have children). For a leaf whose content comes from a "
                "file, use `import_file`. In confirmation mode the call is held for the user's approval; it applies on approval, or returns `rejected` with the user's feedback if declined."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string", "description": "Full markdown body."},
                    "parent_id": {
                        "type": ["string", "null"],
                        "description": (
                            "Placement — required. A parent document id: the tree "
                            "parent for a document (null = {{ROOT}}), or "
                            "the host for a reference (never null). Omitting it "
                            "is invalid."
                        ),
                    },
                    "node_type": {
                        "type": "string",
                        "enum": ["document", "reference"],
                        "default": "document",
                        "description": (
                            "The kind of node to create: \"document\" (default) = "
                            "a tree document; \"reference\" = a leaf attached to "
                            "parent_id (the host). media_type is derived "
                            "server-side, so an audio/image node with no file is "
                            "unexpressible."
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
                            "Apply markdown normalize (reflow wrapped prose, dedent "
                            "list-nested fences) to `content` before writing. Default "
                            "False — do NOT reflow text you authored; pass True only "
                            "when importing a .md file's text that carries meaningful "
                            "hard line breaks."
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
                "Lore History panel. This is the ONLY way to create a table — a pipe "
                "table written into a document body stays plain text."
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
                "Propose moving a document to a new parent and/or position. Move any "
                "node under a different parent — a tree document or an attached file "
                "node, same call. An attached node is visible from its host document "
                "and all of that host's descendants, so hosting it on the project's "
                "index document makes it visible project-wide. parent_id = new parent "
                "(null = project root); after_id = a sibling to land after under the "
                "target parent (null = top). after_id orders tree documents only; "
                "attached nodes are not tree-ordered, so passing it for one is an "
                "error. Omit `node_type` to KEEP the node's kind; pass it to CONVERT "
                "the node in the same structural op: node_type=\"reference\" turns a "
                "document into a reference of parent_id (the host) — it gets the "
                "top key of the host's reference group, after_id and a root parent "
                "are rejected, and a document with children is refused (a "
                "reference cannot be a parent); "
                "node_type=\"document\" turns a reference into a tree document under "
                "parent_id — a file-backed reference (audio/image bytes) is refused. "
                "To reorder in place, pass the current parent_id. NOT reversible "
                "via the Lore History panel (move is structural) — undo by moving back. "
                "In confirmation mode the call is held for the user's approval; it applies on approval, or returns `rejected` with the user's feedback if declined."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "parent_id": {
                        "type": ["string", "null"],
                        "description": (
                            "New parent document id, or null for {{ROOT}}. "
                            "Omitted/null = root. An attached node may not go to the "
                            "root — host it on the index document instead. For "
                            "node_type=\"reference\" this is the HOST (required)."
                        ),
                    },
                    "after_id": {
                        "type": "string",
                        "description": (
                            "A sibling under the target parent to place this document "
                            "after; null = top of the group. Tree documents only."
                        ),
                    },
                    "node_type": {
                        "type": "string",
                        "enum": ["document", "reference"],
                        "description": (
                            "Omit to keep the node's kind (a plain move). "
                            "\"reference\" converts a document into a reference of "
                            "parent_id (the host); \"document\" converts a reference "
                            "into a tree document. A no-op conversion (the kind it "
                            "already is) is a plain move."
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
]

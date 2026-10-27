"""Subtree Markdown tool specs (pure data) — export_subtree_md /
import_subtree_md. Same uniform spec shape as the other modules; the handlers
live in routes.tool_api.subtree, the domain mapper in backend/subtree_md.py
(see SYSTEM: subtree-md).
"""

EXPORT_SUBTREE_MD_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "export_subtree_md",
        "description": (
            "Export a document's whole subtree as ONE zip of Markdown files, "
            "laid out for GitHub/Obsidian: a parent becomes `Title/` with its "
            "body in `INDEX.md`, a leaf becomes `Title.md`, inline image "
            "binaries land in `Title/_assets/` and are linked relatively, "
            "tables are plain GFM, mention links to docs inside the subtree "
            "become relative file links. Returns a short-lived URL to "
            "download the zip — never bytes. Accepted losses: manual sibling "
            "order (export is alphabetical), comments, history, permissions, "
            "non-inline attachments, empty leaves, and links pointing "
            "outside the subtree (they become plain text). The zip re-imports "
            "with import_subtree_md."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "root_id": {
                    "type": "string",
                    "description": (
                        "The document whose subtree to export (itself + all "
                        "descendants)."
                    ),
                },
            },
            "required": ["root_id"],
        },
    },
}

IMPORT_SUBTREE_MD_TOOL: dict = {
    "type": "function",
    "mutating": True,
    "function": {
        "name": "import_subtree_md",
        "description": (
            "Import an md-folder zip (Obsidian-shaped) as a subtree under "
            "`parent_id`. A directory becomes a document (its INDEX.md — or "
            "README.md — is the parent's content), `X.md` becomes a leaf "
            "titled X, `_assets/` files become image/file references hosted "
            "on their directory's document, GFM tables become editable table "
            "blocks, and `[[wiki]]` / relative md links re-link to the "
            "imported documents (same-folder match wins, else a unique "
            "filename match; unresolvable links become plain text). "
            "`.obsidian/` settings are not imported. THE ZIP NEVER RIDES AS "
            "AN ARGUMENT: call once with only `parent_id` to get a "
            "short-lived upload URL, POST the zip to it, then re-call with "
            "the POST response's `staged_id`. In the chat agent's console, "
            "`sandbox_path` (a workspace .zip) imports directly. Read-write "
            "key: applies immediately; undo by deleting the created subtree."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "parent_id": {
                    "type": ["string", "null"],
                    "description": (
                        "The Lore document the imported tree lands under "
                        "(null = {{ROOT}}). Under a subtree-scoped key an "
                        "omitted parent resolves to the scope root."
                    ),
                },
                "staged_id": {
                    "type": "string",
                    "description": (
                        "A zip staged by this tool's upload URL (the POST "
                        "response's staged_id) — the MCP byte channel."
                    ),
                },
                "sandbox_path": {
                    "type": "string",
                    "description": (
                        "Console-only (in-chat agent): a workspace .zip to "
                        "import directly. Not available over MCP."
                    ),
                },
            },
            "required": [],
        },
    },
}

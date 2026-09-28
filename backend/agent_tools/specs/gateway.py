"""Gateway-only tool specs (pure data) — the MCP-only tools that have no
Tool-API route and no agent-surface presence (init, get_file, attach_file, reprocess_file,
preview_extractor). Same uniform spec shape as the other modules; the handlers
live in mcp_gateway.gateway_tools. These describe a PART of a node: the word
"reference" never appears (is_reference is a POSITION, not a type).
"""

INIT_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "init",
        "description": (
            "Call ONCE on connect. Returns the full operating package: protocol, "
            "rules, skill/knowledge indexes, and capabilities. To attach a file: call "
            "attach_file(attach_to, filename) → POST the bytes to the returned url → "
            "poll read_document on the POST response's document_id until "
            "processing_status is terminal (ready | error | null)."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

# Dev/benchmark tool — advertised/routable only under MCP_RUN_EXTRACTOR (the
# gate is applied by mcp_gateway.schemas, the MCP presentation layer).
PREVIEW_EXTRACTOR_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "preview_extractor",
        "description": (
            "Dry-run the extractor for one reference. Resolves params EXACTLY like the "
            "editor (reference → parent source → agent_configs), runs the pipeline, and "
            "returns extracted_data + rendered markdown WITHOUT creating a document. "
            "Dev/benchmark tool (~50s/call, local LLM). Pass config_doc_id to pick one of "
            "several configs; model to override for an experiment. A real run that creates "
            "a document uses POST /api/agent-config/run, NOT this tool (dry_run=false is "
            "rejected — point clients at the REST endpoint instead)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reference_id": {
                    "type": "string",
                    "description": "The reference document id to extract from.",
                },
                "dry_run": {
                    "type": "boolean",
                    "default": True,
                    "description": "Dry-run only (default true). false is rejected — "
                                   "use POST /api/agent-config/run for a real run.",
                },
                "config_doc_id": {
                    "type": "string",
                    "description": "Optional: pick this config when several agent_configs "
                                  "rows exist for the reference's source.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override for an experiment.",
                },
            },
            "required": ["reference_id"],
        },
    },
}

GET_FILE_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "get_file",
        "description": (
            "Get a short-lived URL to download a node's stored original file. Returns a "
            "URL, never bytes. Nodes with no file (`has_file: false` in read_document) "
            "have nothing to download."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "The node whose stored file to download.",
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "Optional save-as name for the client. Defaults to the stored "
                        "sanitized name (the true original is not reliably persisted)."
                    ),
                },
            },
            "required": ["document_id"],
        },
    },
}

# Mutating BY EFFECT: minting the upload URL IS the write (the redeem route has
# no key), so the MCP mutating rate-limit tier applies — see the entry's
# `mutating` flag.
ATTACH_FILE_TOOL: dict = {
    "type": "function",
    "mutating": True,
    "function": {
        "name": "attach_file",
        "description": (
            "Attach a file to a document (`attach_to` = the host node's id). Accepted "
            "types and the size cap for each are in this tool's schema, rendered from "
            "what the server takes. Returns a short-lived upload URL; you POST the bytes "
            "to it yourself — no tool on this surface accepts file content as an "
            "argument, at any size. The POST response contains the NEW node's "
            "`document_id` (the node is created by the POST, not by this call). Text is "
            "derived from the file asynchronously (audio → transcript, .docx → "
            "markdown); poll `read_document` for `processing_status`. If the URL has "
            "expired, call this tool again for a fresh one — nothing was created. For a "
            "leaf whose text you write yourself, use `create_document` with "
            "`node_type=\"reference\"` and `parent_id` = the host instead; this tool is "
            "only for content that comes from a file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "attach_to": {
                    "type": "string",
                    "description": "Host document id the new file node attaches to.",
                },
                "filename": {
                    "type": "string",
                    "description": (
                        "Source filename. Its extension picks the media branch and is "
                        "confirmed against the uploaded bytes — a mismatch is rejected."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Title; defaults to the filename.",
                },
            },
            "required": ["attach_to", "filename"],
        },
    },
}

# Mutating by effect (a content wipe + queue), same as attach_file.
REPROCESS_FILE_TOOL: dict = {
    "type": "function",
    "mutating": True,
    "function": {
        "name": "reprocess_file",
        "description": (
            "Re-run the conversion of a node whose `processing_status` is `error` or "
            "`processing` and whose text is empty (`processing` covers a run whose worker "
            "died). Refused otherwise, because it wipes existing text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "The node whose conversion to re-run.",
                },
            },
            "required": ["document_id"],
        },
    },
}

"""Pipeline agent tool CONSTANTS: the `pipelines` skill's pack. Pure data —
imports nothing (leaf), mirroring specs/skills.py.
"""

# ARCH: the five tools are agent-only (surfaces agent + tool_api, never MCP).
# Why not MCP: an MCP key is subtree-scoped and has no approval card — the
# driver's mid-turn approval round IS the consent axis for the three mutating
# tools (bind = consent to every future run), and launching an ongoing model
# cost from an external client is a product decision not taken here. The
# operator may extend later; nothing in the handlers is MCP-shaped.
# The tools are packed by the shipped `pipelines` skill
# (backend/configs/skill_pipelines.md): a packed tool is neither listed nor
# callable until the skill loads (the driver serves core = served minus the
# union of enabled skills' packs) — the same way `search_my_chats` is packed by
# skill_chat_recall.md. Unlike save_skill there is NO core exception: a model
# that skips the skill has no fallback that writes a wrong binding — it simply
# cannot bind.
LIST_PIPELINES_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "list_pipelines",
        "description": (
            "List the pipeline bindings of this project: extractor bindings "
            "(a source document's new transcriptions are extracted into child "
            "documents under a target) and digest bindings (periodic reports on "
            "a theme). Each row names its source/config/target documents by id "
            "AND title, the trigger, model, title template, its schedule "
            "(interval, enabled, last outcome) and its last recorded run. "
            "Optional `document_id` narrows to bindings on that source "
            "document. Read this FIRST — before binding, running or "
            "diagnosing — so you name an existing binding instead of "
            "duplicating it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": (
                        "Only bindings whose source document is this one "
                        "(omit: every binding of the project)."
                    ),
                },
            },
            "required": [],
        },
    },
}

BIND_PIPELINE_TOOL: dict = {
    "type": "function",
    "mutating": True,
    "function": {
        "name": "bind_pipeline",
        "description": (
            "Bind a pipeline: link an extractor or digest config document to a "
            "source document, writing its output under a target document. "
            "Creating and re-configuring are the SAME call — an existing "
            "binding on the same source+kind is updated in place, so "
            "\"configure\" is a re-bind. A binding is CONSENT TO EVERY FUTURE "
            "RUN: an extractor fires on each new transcription of the source's "
            "references, a digest on its schedule and on new references. "
            "`schedule` (digest only) also upserts the binding's schedule row; "
            "null leaves it, enabled:false pauses it. Returns the binding and "
            "its schedule. Verify with list_pipelines."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "The SOURCE document (its references feed the pipeline; for a digest this is the theme document).",
                },
                "kind": {
                    "type": "string",
                    "enum": ["extractor", "digest"],
                    "description": "Extractor: per-reference structured extraction. Digest: periodic theme report.",
                },
                "config_doc_id": {
                    "type": "string",
                    "description": "The config document (authored per the extractor-config / digest-config skill).",
                },
                "target_doc_id": {
                    "type": "string",
                    "description": "Output parent: extracted child documents / digest reports land under it.",
                },
                "title_template": {
                    "type": "string",
                    "description": "Optional title template for output documents (e.g. \"{title} — {yyyy}-{mm}-{dd}\").",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override for the runs (omit: the config's / system default).",
                },
                "subject_user_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Digest only: whose chats the digest reads (omit: only the creator's).",
                },
                "schedule": {
                    "type": "object",
                    "properties": {
                        "interval_s": {
                            "type": "integer",
                            "minimum": 60,
                            "description": "Seconds between scheduled runs (digest only).",
                        },
                        "enabled": {
                            "type": "boolean",
                            "description": "false pauses the schedule without deleting it.",
                        },
                    },
                    "required": ["interval_s"],
                    "description": "Digest only: upsert the binding's schedule (interval in seconds). null leaves the existing schedule untouched.",
                },
                "apply": {
                    "type": "string",
                    "enum": ["confirm", "auto"],
                    "description": "confirm (default) asks the user via the approval card; auto applies when allowed.",
                },
            },
            "required": ["document_id", "kind", "config_doc_id", "target_doc_id"],
        },
    },
}

UNBIND_PIPELINE_TOOL: dict = {
    "type": "function",
    "mutating": True,
    "function": {
        "name": "unbind_pipeline",
        "description": (
            "Remove a pipeline binding (soft-delete). A digest binding's "
            "schedule is deleted with it, so nothing keeps firing into a "
            "missing binding; an extractor has no schedule of its own. The "
            "already-written output documents stay. Re-binding later is a "
            "fresh bind_pipeline call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "config_id": {
                    "type": "string",
                    "description": "The binding's config_id (from list_pipelines).",
                },
                "apply": {
                    "type": "string",
                    "enum": ["confirm", "auto"],
                    "description": "confirm (default) asks the user via the approval card; auto applies when allowed.",
                },
            },
            "required": ["config_id"],
        },
    },
}

RUN_PIPELINE_TOOL: dict = {
    "type": "function",
    "mutating": True,
    "function": {
        "name": "run_pipeline",
        "description": (
            "Run a pipeline NOW. A digest binding runs immediately (writes a "
            "report only if there is new material). An extractor binding runs "
            "on ONE reference: `reference_id` is required and must be a "
            "reference attached to the binding's source document. The run is "
            "async — check its outcome with pipeline_runs. A run posted while "
            "another is pending returns already_pending, not a second run. "
            "This launch is a model run (an 'ai'-tier act)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "config_id": {
                    "type": "string",
                    "description": "The binding to run (from list_pipelines).",
                },
                "reference_id": {
                    "type": "string",
                    "description": "Extractor only, required: the reference to extract from (attached to the binding's source document).",
                },
                "apply": {
                    "type": "string",
                    "enum": ["confirm", "auto"],
                    "description": "confirm (default) asks the user via the approval card; auto applies when allowed.",
                },
            },
            "required": ["config_id"],
        },
    },
}

PIPELINE_RUNS_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "pipeline_runs",
        "description": (
            "The recorded runs of this project's pipelines, newest first: "
            "trigger (manual / event / schedule), status (queued / running / "
            "ok / error / skipped), the error message on a failure, and the "
            "output document id on success. `skipped` means the pipeline ran "
            "and deliberately produced nothing (e.g. an empty digest window). "
            "With `config_id` only that binding's runs. This is how you "
            "VERIFY what you bound or ran — read it after run_pipeline and "
            "after a transcription should have fired."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "config_id": {
                    "type": "string",
                    "description": "Only this binding's runs (omit: every in-scope binding).",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Maximum runs to return (default 20, newest first).",
                },
            },
            "required": [],
        },
    },
}

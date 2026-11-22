"""Document-property agent tool CONSTANTS: the `tasks` skill's pack — the
query half (find_documents) and the task-loop half (next_task, claim_task).
Pure data — imports nothing (leaf), mirroring specs/pipelines.py.

# ARCH: unlike the pipelines pack these tools ARE public — surfaces agent +
# tool_api + mcp. Why MCP is the point: the feature's goal is an EXTERNAL
# coding agent on an MCP key working the project's backlog, and the claim is
# coordination, not access control (every write is checkpointed and
# reversible). The shipped `tasks` skill (backend/configs/skill_tasks.md)
# packs them out of the in-app core; the skill-disabled gate is per-handler
# (_refuse_tasks_disabled), not a surface difference — MCP and the Tool-API
# serve the tools regardless of the pack (INVARIANT(security) in
# routes/tool_api/_common.py). claim_task carries `apply` (the in-app driver
# can hold a claim for its approval card; MCP force-applies as always).
"""
FIND_DOCUMENTS_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "find_documents",
        "description": (
            "List this project's documents by the properties in their YAML "
            "header — the `---` frontmatter block at the very top of the "
            "content (documents without one never match). `where` is "
            "{key: value | [values]}: exact equality on top-level header keys "
            "only; a list value means any-of. Equality is exact on the JSON "
            "value: `priority: 1` matches 1, not \"1\"; `done: true` matches "
            "true, not \"true\". Dates and datetimes in headers are stored as "
            "ISO 8601 UTC strings (`2026-11-22`, `2026-11-22T10:00:00+00:00` "
            "— a naive datetime is UTC), so match them as strings. Returns "
            "{results: [{id, title, parent_id, props}], header_errors: "
            "[{id, title, error}]} — every visible document whose header "
            "failed to parse is listed in header_errors (capped at 20), so a "
            "typo'd document is reported, never silently absent. In a YAML "
            "flow sequence a Lore link must be quoted (`depends: "
            "[\"[Схема](doc:id)\"]`), or the header itself fails to parse."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "where": {
                    "type": "object",
                    "description": (
                        "Top-level header keys to match by exact equality; a "
                        "list value matches any of its entries. Omit to list "
                        "every visible header-bearing document."
                    ),
                    "additionalProperties": True,
                },
                "under": {
                    "type": "string",
                    "description": (
                        "Only documents inside this document's subtree "
                        "(itself + descendants). Omit: the whole project."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Maximum results (default 50).",
                },
            },
            "required": [],
        },
    },
}

NEXT_TASK_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "next_task",
        "description": (
            "Pick the next available task from this project's task documents "
            "(documents whose YAML header has `type: task`). Requires a "
            "schema document: a visible document whose header has `type: "
            "schema`, `schema_for: task`, `statuses: [...]`, `ready:` and "
            "`claimed:` statuses and `closed: [...]` (optional: `initial:`, "
            "`priority: [...]` — most urgent value FIRST, served first), with "
            "the project's task rules in its body. A task whose `priority` is "
            "not in that list is reported in `invalid`, not offered. "
            "Returns {task, schema_id, initial, counts: {available, blocked, "
            "invalid, stale}, invalid: [...], blocked: [...]}. `task` is null "
            "when nothing is available. Available = ready, or claimed with an "
            "expired lease (`stale: true`), and every `depends` entry (a "
            "quoted Lore link like \"[t](doc:id)\" or a bare document id) "
            "resolved to a visible document whose status is in the schema's "
            "`closed` list. `initial` is the status a NEWLY authored task "
            "gets. A task offered by next_task may already be re-claimed by "
            "the time you call claim_task — the 409 already_claimed answer "
            "tells you to call next_task again. Before the first task, read "
            "the `tasks` skill (MCP: `init` → `shipped_skills`) and the "
            "schema document (`schema_id`)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "under": {
                    "type": "string",
                    "description": (
                        "Only tasks inside this document's subtree "
                        "(itself + descendants). Omit: the whole project."
                    ),
                },
            },
            "required": [],
        },
    },
}

CLAIM_TASK_TOOL: dict = {
    "type": "function",
    "mutating": True,
    "function": {
        "name": "claim_task",
        "description": (
            "Claim (or renew) a task document: sets `status:` to the "
            "schema's `claimed` status and upserts `assignee` and "
            "`claimed_until` in its YAML header — one header-only splice, "
            "checkpointed and reversible in the Lore History panel. Requires "
            "the same schema document as next_task. The lease is 4 hours; "
            "renew by calling again with the same `agent_name`. Outcomes: "
            "`claimed`, `renewed` (same agent), `taken_over` (an expired "
            "lease); 409 `already_claimed` (call next_task again for another "
            "task), 409 `not_ready` / `not_a_task`. The claim is "
            "coordination, not access control: finish by writing the work "
            "log under the heading the schema body names and setting a "
            "review status — never a `closed` status. Before the first "
            "task, read the `tasks` skill (MCP: `init` → `shipped_skills`) "
            "and the schema document."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "The task document to claim.",
                },
                "agent_name": {
                    "type": "string",
                    "description": (
                        "Who is claiming, written into the header's "
                        "`assignee`: letters and digits of any script, "
                        "spaces and ._@-, starting with a letter or digit, "
                        "up to 64 characters (no newline, no colon)."
                    ),
                },
                "apply": {
                    "type": "string",
                    "enum": ["confirm", "auto"],
                    "description": (
                        "confirm (default) asks the user via the approval "
                        "card; auto applies when allowed."
                    ),
                },
            },
            "required": ["document_id", "agent_name"],
        },
    },
}

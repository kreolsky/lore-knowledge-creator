"""Document-property agent tool CONSTANTS: the `tasks` skill's pack — the
query half (find_documents) and the task-loop half (next_task, claim_task,
set_task_status).
Pure data — imports nothing (leaf), mirroring specs/pipelines.py.

# ARCH: unlike the pipelines pack these tools ARE public — surfaces agent +
# tool_api + mcp. Why MCP is the point: the feature's goal is an EXTERNAL
# coding agent on an MCP key working the project's backlog, and the claim is
# coordination, not access control (every write is checkpointed and
# reversible). The shipped `tasks` skill (backend/configs/skill_tasks.md)
# packs them out of the in-app core; the skill-disabled gate is per-handler
# (_refuse_tasks_disabled), not a surface difference — MCP and the Tool-API
# serve the tools regardless of the pack (INVARIANT(security) in
# routes/tool_api/_common.py). claim_task and set_task_status carry `apply`
# (the in-app driver can hold the write for its approval card; MCP
# force-applies as always).
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
            "`priority: [...]` — most urgent value FIRST, served first, "
            "`archive:` — a document link closed top-level tasks move under), "
            "with the project's task rules in its body. A task whose "
            "`priority` is not in that list is reported in `invalid`, not "
            "offered. Pass `agent_name`: a ready task whose `assignee` is "
            "someone else (any assignee, without `agent_name`) is not offered "
            "and is listed in `reserved`; actively claimed tasks are listed in "
            "`in_progress` — resume your own from there. "
            "Returns {task, schema_id, initial, counts: {available, blocked, "
            "invalid, reserved, in_progress, stale}, invalid: [...], blocked: "
            "[...], reserved: [{id, title, assignee}], in_progress: [{id, "
            "title, assignee, claimed_until}]}. `task` is null "
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
                "agent_name": {
                    "type": "string",
                    "description": (
                        "Your name, as you pass it to claim_task — tasks "
                        "assigned to it are offered to you."
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
            "task), 409 `reserved` (assigned to someone else), 409 "
            "`not_ready` / `not_a_task`. The claim is coordination, not "
            "access control: finish by writing the work log under the "
            "heading the schema body names and calling set_task_status with "
            "a review status — close only when the person asks. Before the first "
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

SET_TASK_STATUS_TOOL: dict = {
    "type": "function",
    "mutating": True,
    "function": {
        "name": "set_task_status",
        "description": (
            "Set a task document's status — hand it back (a review status) or "
            "close it (a `closed` status, ONLY when the person asks). Replaces "
            "`status:` and removes `claimed_until` in its YAML header; "
            "`assignee` stays as the record of who worked it. One header-only "
            "splice, checkpointed and reversible in the Lore History panel. "
            "Closing a top-level task moves it LAST under the schema's "
            "`archive` document (a subtask closes in place and goes with its "
            "parent). Returns {outcome: set, document_id, status, "
            "previous_status, archived_to}. 409 `use_claim_task` (the claimed "
            "status belongs to claim_task), `already_claimed` (another agent's "
            "live lease), `no_change`, `open_subtasks` (close them first), "
            "`archive_not_visible`, `unknown_status`. A failure after a close "
            "is completed by calling again with the same status. Never close "
            "a task by editing its header."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "The task document.",
                },
                "status": {
                    "type": "string",
                    "description": (
                        "A status from the schema's `statuses`, not the "
                        "`claimed` one."
                    ),
                },
                "agent_name": {
                    "type": "string",
                    "description": (
                        "Who is acting, as passed to claim_task (same "
                        "grammar); recorded in the History checkpoint."
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
            "required": ["document_id", "status", "agent_name"],
        },
    },
}

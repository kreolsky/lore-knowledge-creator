"""Skill-authoring agent tool CONSTANTS: save_skill. Pure data — imports
nothing (leaf), mirroring specs/memory.py.
"""

# ARCH: save_skill is agent-only (surfaces agent + tool_api, never mcp) — the
# MCP surface must not author the agent's own config (no doc under the system
# root except Memory — personas, rules, knowledge, skills — is editable over
# MCP; see INVARIANT(security) in agent/apply_policy.py and the is_system
# refusal cell in routes/tool_api/_common.py).
# Always served: no external dependency to gate on (unlike sandbox_* /
# web_search). CORE on purpose, not packed into the skill-authoring skill — a
# model that skips the skill load still finds the tool by name and gets
# server-assembled frontmatter; packed, it would fall back to create_document
# and hand-written YAML the plugin silently skips.
SAVE_SKILL_TOOL: dict = {
    "type": "function",
    "mutating": True,
    "function": {
        "name": "save_skill",
        "description": (
            "THE way to save a project skill — what this chat learned, or an "
            "update to an existing one (same `name` = updated in place). The "
            "server files it under the project's Skills folder and assembles the "
            "YAML frontmatter itself: never create it with create_document or "
            "write frontmatter by hand. `spec` is stored as a Spec child document "
            "the skill lists but does not load (read_document it when needed); "
            "`files` copies sandbox files into the skill as child documents, "
            "later materialized by `sandbox_fetch_skill`. On an update, omit "
            "`spec` / `files` to keep them. A shipped skill's name is refused "
            "(409). Returns {status, document_id, spec_doc_id, name, files}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
                    "description": "Kebab-case; the catalog and upsert key.",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "ONE line: when to load this skill, as a trigger — 'Use "
                        "when the user … — «RU phrases», EN phrases'. The only "
                        "line the catalog shows."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": (
                        "Markdown recipe served on load: When / Inputs / Steps / "
                        "Output. Reference material goes in `spec`, not here."
                    ),
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Tool pack the skill activates (omit = prose-only). "
                        "Served tool names only — `sandbox_bash`, not `bash`."
                    ),
                },
                "spec": {
                    "type": "string",
                    "description": (
                        "Reference facts that cannot be re-derived (URL patterns, "
                        "auth quirks, response fields, pitfalls) — never the "
                        "script, that goes in `files`."
                    ),
                },
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "pattern": "^(scripts|references)/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?$",
                                "description": "Path inside the skill bundle.",
                            },
                            "sandbox_path": {
                                "type": "string",
                                "description": "Its path in your sandbox workspace.",
                            },
                        },
                        "required": ["path", "sandbox_path"],
                    },
                    "description": (
                        "Text files (scripts, references) from your workspace. On "
                        "an update pass only changed files."
                    ),
                },
                "apply": {
                    "type": "string",
                    "enum": ["confirm", "auto"],
                    "default": "confirm",
                    "description": (
                        "confirm (default) holds it for the user's approval; auto "
                        "applies directly (full access + opted-in)."
                    ),
                },
            },
            "required": ["name", "description", "body"],
        },
    },
}

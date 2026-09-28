"""Skill-authoring agent tool CONSTANTS: save_skill. Pure data — imports
nothing (leaf), mirroring specs/memory.py.
"""

# ARCH: save_skill is agent-only (surfaces agent + tool_api, never mcp) — the
# MCP surface must not author the agent's own config (rules/skills are not
# editable over MCP; see routes/tool_api/_common.py's is_system refusal cell).
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
            "Save what this chat just learned as a project skill, or improve one "
            "already saved (same name = same skill, updated in place). The server "
            "places the skill document under the project's Skills folder and "
            "assembles its YAML frontmatter itself — never write frontmatter by "
            "hand. `name` is kebab-case (`^[a-z0-9]+(?:-[a-z0-9]+)*$`); "
            "`description` is ONE line, the catalog trigger: when the model "
            "should load this skill, phrased the user would phrase it (RU and EN "
            "phrases); `body` is the markdown recipe the model will read on load "
            "(sections When / Inputs / Steps / Output). `tools` optionally names "
            "the tool pack the skill activates — every name must be a served "
            "tool. `spec` optionally holds reference material (URL patterns, "
            "auth quirks, response field names, pitfalls — facts only, never "
            "the script): it is stored as a Spec child "
            "document the skill lists but does not load — the body tells the "
            "model to read_document it when needed, so it costs nothing per "
            "load. OMIT `spec` when improving a skill whose Spec should stay; "
            "pass it to replace. `files` attaches the skill's scripts from your "
            "sandbox workspace: each {path, sandbox_path} is read from the "
            "workspace and stored as a child document titled `path` "
            "(`scripts/run.py`, `references/api.md`), text verbatim; "
            "`sandbox_fetch_skill` later materializes them for any member. "
            "Omit `files` to keep the existing ones on an upsert. A name "
            "matching a shipped skill is refused (409). In confirmation mode "
            "the call is held for the user's approval; it applies on "
            "approval. Returns {status, doc_id, spec_doc_id, name, files}."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
                    "description": (
                        "The skill name, kebab-case — the catalog key and the "
                        "upsert key (a second call with the same name updates "
                        "the same document)."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        "ONE line, no newlines: when to load this skill, written "
                        "as a trigger — 'Use when the user … — «RU phrases», EN "
                        "phrases'. This is the only line the catalog shows."
                    ),
                },
                "body": {
                    "type": "string",
                    "description": (
                        "The markdown recipe served on load. Sections: When / "
                        "Inputs / Steps / Output. Do NOT inline reference "
                        "material here — it rides every load; put it in `spec`."
                    ),
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "The tool pack this skill activates (empty or omitted = "
                        "prose-only skill). Every name must be a served tool "
                        "name — the sandbox console is `sandbox_bash`, not "
                        "`bash`."
                    ),
                },
                "spec": {
                    "type": "string",
                    "description": (
                        "Optional reference material stored as a Spec child "
                        "document, read on demand. The facts that cannot be "
                        "re-derived — URL patterns, auth quirks, response field "
                        "names, pitfalls. Never the script — that goes in "
                        "`files`. Omit to keep an existing Spec on an upsert."
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
                                "description": (
                                    "The file's path inside the skill bundle: "
                                    "`scripts/<name>` or `references/<name>` "
                                    "(one optional subdirectory)."
                                ),
                            },
                            "sandbox_path": {
                                "type": "string",
                                "description": (
                                    "Where the file sits in your sandbox "
                                    "workspace (as written by sandbox_bash)."
                                ),
                            },
                        },
                        "required": ["path", "sandbox_path"],
                    },
                    "description": (
                        "The skill's text files (scripts, reference material) "
                        "read from your workspace and stored as child "
                        "documents. Pass again on an upsert only when a file "
                        "changed; omit to keep the existing files."
                    ),
                },
                "apply": {
                    "type": "string",
                    "enum": ["confirm", "auto"],
                    "default": "confirm",
                    "description": (
                        "confirm (default) holds the save for the user's "
                        "approval; auto applies it directly (requires full "
                        "access + opted-in)."
                    ),
                },
            },
            "required": ["name", "description", "body"],
        },
    },
}

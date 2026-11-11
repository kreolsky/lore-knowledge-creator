"""Media / sandbox / exec agent tool CONSTANTS — the agent-only surface that
reaches internal services an external MCP client cannot. Pure data — imports
nothing from the agent package (leaf).
"""

from artifacts import html_import_sentence

# import_file is agent-ONLY now (the in-chat
# agent keeps it with sandbox_path / attachment_index — there the driver reads the
# file, so D1 already holds). It was the LAST entry of AGENT_TOOLS; moved OUT so the
# MCP gateway (which derives its surface FROM AGENT_TOOLS) no longer advertises or
# routes it. The mcp_hidden projection is gone with it — a tool that never reaches
# MCP needs no schema projection. Same posture as SANDBOX_* / GENERATE_IMAGE.
IMPORT_FILE_TOOL: dict = {
    "type": "function",
    "mutating": True,
    "function": {
        "name": "import_file",
        "description": (
            "Import a FILE as a new document or reference via the import pipeline "
            "(markdown normalize for .md, image extraction) or the binary save path "
            "(image → thumbnail, audio → transcribed, .zip → a downloadable file "
            "reference). Set node_type to "
            "\"reference\" to attach the result to a host document (parent_id) as "
            "a reference; otherwise (default) it lands as a new document under "
            "parent_id. Pass `sandbox_path` for a file you produced in the "
            "sandbox console, or `message_id` + `attachment_index` for an image "
            "attached to a chat message — its address is the "
            "`[attached images: message_id=…, count=N]` marker under the current "
            "message, an `images: N` marker on a chat-recall read, or "
            "`view_chat_image`'s meta (the index is the same 0-based index "
            "view_chat_image serves). For an image from another chat, view it with "
            "`view_chat_image` first and save that one with the same "
            "`message_id` and `index`. Applies immediately. For very large files "
            "prefer the upload widget.\n"
            # The create-vs-upload boundary
            # is "text I authored" → create_document (may propose); "bytes I am
            # importing" → import_file (applies immediately). Plain `content`
            # (authored text) is NOT an input here — use create_document for that,
            # passing `normalize: true` if the text is a .md file's body.
            # content_base64 is
            # NOT an input — you cannot see file bytes, so never emit base64 into
            # this call. Bytes arrive only via sandbox_path (a file you produced)
            # or message_id + attachment_index (a stored chat image).
            # To share a bundle of sandbox files (e.g. a built web page), pack it
            # with `python -m zipfile -c out.zip <paths…>` — the console has NO
            # zip binary, python's stdlib is the route — then import the .zip as
            # a reference; the user gets a downloadable archive.
            "Binary image/audio/archive is not a document — set "
            "node_type=\"reference\" for those."
            + html_import_sentence()
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": (
                        "Source filename. A .md/.markdown suffix on a text file runs "
                        "markdown normalize; the extension picks image vs audio for "
                        "binary, and .zip means a downloadable archive reference. "
                        "On the chat-image path only the STEM is used — the "
                        "extension comes from the stored image."
                    ),
                },
                "node_type": {
                    "type": "string",
                    "enum": ["document", "reference"],
                    "default": "document",
                    "description": (
                        "The kind of node to create: \"document\" (default) → a new "
                        "document under parent_id (the tree parent); \"reference\" → "
                        "a reference attached to parent_id (the host). Binary "
                        "image/audio requires node_type=\"reference\"."
                    ),
                },
                "sandbox_path": {
                    "type": "string",
                    "description": (
                        "A file already in your sandbox workspace (e.g. a report "
                        "you generated, a chart you plotted). Mutually exclusive with "
                        "message_id + attachment_index. This is the ONLY way to "
                        "import a file you produced — you cannot see file bytes, so "
                        "never emit base64. To share several files at once, pack "
                        "them with `python -m zipfile -c out.zip <paths…>` (no zip "
                        "binary on the console) and pass the .zip here."
                    ),
                },
                "message_id": {
                    "type": "string",
                    "description": (
                        "With attachment_index (mutually exclusive with "
                        "sandbox_path): the message carrying the image to save. "
                        "Take it from the `[attached images: message_id=…]` marker "
                        "under the current message, an `images: N` marker on a "
                        "chat-recall read, or `view_chat_image`'s meta."
                    ),
                },
                "attachment_index": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "With message_id: the image's 0-based position among that "
                        "message's images (the same index `view_chat_image` "
                        "serves). One image per call — save several with several "
                        "calls."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Title; defaults to the filename stem.",
                },
                "parent_id": {
                    "type": ["string", "null"],
                    "description": (
                        "Placement, discriminated by node_type: the tree parent for "
                        "a document, or the HOST document for a reference (required "
                        "when node_type=\"reference\")."
                    ),
                },
            },
            "required": ["filename"],
        },
    },
}

# ARCH: defined OUTSIDE AGENT_TOOLS on purpose — AGENT_TOOLS is what mcp-gateway
# turns into its public tool list, and this must never appear there. Only
# agent_toolset() (the agent path) appends it. See the seam note on agent_toolset.
SANDBOX_TOOL: dict = {
    "type": "function",
    # mutating=True buys no proxy timeout in the dsh driver (a long command is not
    # cut at the read-tool bound) and keeps it out of the read-only line. It does NOT drag in the proposal/confirm path
    # — that is resolved by the document endpoints via resolve_apply_mode, not by
    # membership here — and it is EXEMPT from the per-target repeat bound
    # (the turn telemetry's per-target repeat bound). Why the exemption: re-running `pytest` after a fix is
    # normal shell work, whereas an identical repeated document edit is a stuck loop.
    "mutating": True,
    "function": {
        "name": "sandbox_bash",
        "description": (
            "Run a bash command on your sandbox: a Linux host with Python, pip, gcc "
            "and git.\n"
            # {{SANDBOX_ENV}} is filled at render time by agent_toolset
            # (agent.tools) from the box's provisioned-environment manifest
            # (routes.tool_api.sandbox.transport.sandbox_env_fill): the ACTUAL
            # python/node/chromium versions and the pinned Python packages WITH
            # versions — pandas 2 → 3 API breaks matter to the model. Only
            # agent_toolset fills it: this tool never reaches MCP
            # (surfaces=_AGENT_HTTP), so no MCP path can carry the fill.
            "{{SANDBOX_ENV}}\n"
            "Use it to write and run scripts, do calculations, process "
            "data and install packages (`pip install X`) — it has internet "
            "access. It is a real console, so re-running a command after a fix "
            "is normal.\n"
            # State drift across sessions is the known trap here: files
            # persist, so a stale artifact silently poisons a later run. Naming the
            # escape hatch in the description is what lets the agent resolve that
            # itself instead of looping.
            "Each call is a SEPARATE shell: the working directory, environment "
            "variables and Python variables do NOT carry over between calls — chain "
            "steps with `&&` in one command, or write a script and run it. Files in "
            "your workspace DO persist across calls and chat sessions, which means a "
            "leftover file from earlier work can make a run non-reproducible; ask the "
            "user to restart the sandbox if the environment seems wedged.\n"
            # To get a file OUT of the workspace, name its path as `sandbox_path` on
            # import_file — do NOT base64 it into the request (it is capped and slow).
            # Naming the route here is what stops the agent from reaching for
            # base64-over-exec, the only channel it had before.
            "To land a file you produced here as a document or reference, pass its "
            "workspace path as `sandbox_path` to import_file (node_type picks "
            "document vs reference) — do not base64-encode it into the request. "
            "To see an image you produced, use sandbox_view_image.\n"
            # The result contract: status + bounded slices + the full logs'
            # address. `timeout` is a WAIT, not a lifetime — promotion is the
            # behavior that replaces the old kill-at-60s, so it must be stated,
            # not implied, or the model reads a promoted run as dead.
            "The result carries {status, exit_code, stdout, stderr, "
            "stdout_bytes, stderr_bytes, truncated, run_id, log_dir}: stdout/"
            "stderr are bounded head+tail slices of the run's logs (`truncated: "
            "true` names elision — the FULL logs are out.log and err.log inside "
            "log_dir; read them with another command). `timeout` is how long "
            "this call WAITS: at the deadline a still-running command is NOT "
            "killed — it is promoted to a background run and the result is "
            "{status: \"running\", run_id}, to collect with sandbox_run_status "
            "or stop with sandbox_run_kill. A run whose .runs dir the command "
            "itself deleted (git clean -fdx, rm -rf .runs) reports "
            "{status: \"killed\"} — its process may in fact keep running, "
            "and it 404s on sandbox_run_status and sandbox_run_kill.\n"
            "For work known to run LONGER than the 300 s wait cap, pass "
            "`detach: true` instead: it starts the run in the background and "
            "returns {status:\"running\", run_id, log_dir} at once. Either way the run "
            "is bounded by the detached ceiling (hours), not `timeout`. The "
            "workspace is ONE file tree shared with every other call, so do "
            "NOT launch two runs that would fight over the same checkout or "
            "files — the runs are not isolated.\n"
            "You are a non-root user: no sudo, no apt-get. The sandbox cannot reach "
            "Lore itself or any internal service — use the document tools for that."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to run.",
                },
                "timeout": {
                    "type": "number",
                    "description": (
                        "Seconds this call WAITS for the command (default 60, "
                        "max 300). At the deadline a still-running command is "
                        "promoted to a background run — NOT killed — and the "
                        "result carries its run_id. Ignored when detach is true."
                    ),
                },
                "detach": {
                    "type": "boolean",
                    "description": (
                        "Start the command in the background and return "
                        "{status:\"running\", run_id} at once (default false), "
                        "for a command known to outlive the 300 s wait cap. "
                        "The run is bounded by the detached ceiling, not "
                        "`timeout`; collect it with `sandbox_run_status` in "
                        "this or a later turn."
                    ),
                },
                "with_external_agent_key": {
                    "type": "boolean",
                    "description": (
                        "Hand THIS run the external agent's model credential, as the "
                        "environment variable LORE_EXTERNAL_AGENT_KEY (default false). "
                        "Set it only for a command that launches the external agent, "
                        "and reference the variable by name — never echo, print, copy "
                        "or interpolate its value, and never pass a credential as part "
                        "of `command`."
                    ),
                },
            },
            "required": ["command"],
        },
    },
}

# ARCH: defined OUTSIDE AGENT_TOOLS for the same reason
# as SANDBOX_TOOL — it is console-only (internal whole-project key) and must never
# reach the MCP surface. Only agent_toolset() (the agent path) appends it.
SANDBOX_RUN_STATUS_TOOL: dict = {
    "type": "function",
    # read-only: polls a run's directory and touches nothing — outside
    # MUTATING_TOOLS by design, so polling keeps the read-tool timeout and is served
    # in the read-only line, and several runs can be in flight at once.
    "mutating": False,
    "function": {
        "name": "sandbox_run_status",
        "description": (
            "Poll a run started with `sandbox_bash` — detached, or a foreground "
            "call that was promoted at its wait deadline. Returns {status: "
            "running|exited|killed, run_id, log_dir, exit_code, out, err, "
            "truncated}: "
            "`out`/`err` are capped tails of the run's stdout/stderr logs "
            "(out.log / err.log inside its log_dir), and `truncated: true` "
            "means a real log is longer than its tail. `killed` with exit_code "
            "124/137 means the run hit its wall-clock ceiling.\n"
            # The busy-poll warning is load-bearing: the repeat guard reminds on
            # identical consecutive calls and the whole-turn wall-clock deadline
            # bounds a wait-here loop, so an unfinished run is collected in the
            # NEXT turn, not waited out in this one. Also names the missing-run
            # 404 so the model does not read it as a wedged sandbox.
            "Do NOT busy-wait on this in the current turn — the per-turn tool-call "
            "ceiling will stop you with the run still going. Poll a few times, and "
            "if it is still `running`, end your turn and collect it in the NEXT "
            "turn (or stop it with sandbox_run_kill). A 404 means no such run "
            "(already GC'd, its .runs dir deleted, or never started)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": (
                        "The run_id from `sandbox_bash`'s result — a detached "
                        "spawn or a promoted foreground call, both carry one."
                    ),
                },
            },
            "required": ["run_id"],
        },
    },
}

# ARCH: defined OUTSIDE AGENT_TOOLS for the same reason
# as SANDBOX_TOOL — it is console-only (internal whole-project key) and must never
# reach the MCP surface. Only agent_toolset() (the agent path) appends it.
SANDBOX_RUN_KILL_TOOL: dict = {
    "type": "function",
    # mutating=False on purpose: it only ever signals the caller's OWN run (the
    # workspace is key-derived), so it needs no `apply` and keeps the read-tool
    # timeout — the same class as sandbox_run_status.
    "mutating": False,
    "function": {
        "name": "sandbox_run_kill",
        "description": (
            "Stop a run started with `sandbox_bash` — detached, or a foreground "
            "call that was promoted at its wait deadline (both results carry "
            "run_id). Idempotent: an already-finished run is a no-op returning "
            "{status: \"exited\", exit_code}; anything else signals the run's "
            "whole process group (TERM, then KILL after a short grace) and "
            "returns {status: \"killed\"}. An unknown run_id is a 404 — including "
            "a run whose .runs dir was deleted: its process may still be "
            "running, but it can no longer be collected or stopped from here."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": (
                        "The run_id from `sandbox_bash`'s result (foreground or "
                        "detached)."
                    ),
                },
            },
            "required": ["run_id"],
        },
    },
}

# ARCH: defined OUTSIDE AGENT_TOOLS for the same reason
# as SANDBOX_TOOL — it is console-only (internal whole-project key) and must never
# reach the MCP surface. Only agent_toolset() (the agent path) appends it.
SANDBOX_VIEW_IMAGE_TOOL: dict = {
    "type": "function",
    # mutating=False: it only reads the caller's own workspace (key-derived) —
    # the same class as sandbox_run_kill.
    "mutating": False,
    "function": {
        "name": "sandbox_view_image",
        "description": (
            "Look at an image file in your sandbox workspace — e.g. check a chart "
            "or screenshot you produced before publishing it with import_file. "
            "png, jpeg, gif or webp; rasterize html with Playwright first. View "
            "only what you need to check — images stay in the conversation. Keep "
            "charts/screenshots at normal screen size."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path of the image in your workspace (relative or absolute).",
                },
            },
            "required": ["file_path"],
        },
    },
}

# ARCH: defined OUTSIDE AGENT_TOOLS for the same reason
# as SANDBOX_TOOL — it is console-only (internal whole-project key) and must never
# reach the MCP surface. Only agent_toolset() (the agent path) appends it.
SANDBOX_FETCH_REFERENCE_TOOL: dict = {
    "type": "function",
    # mutating=True for the SAME property as sandbox_bash (no proxy timeout),
    # but is NOT exempt from the per-target repeat bound: ref_id is its only
    # argument, so the generic per-target repeat fallback already keys on it,
    # and a 3rd identical fetch of one ref_id is the stuck-loop signature (the
    # destination is deterministic and a re-fetch overwrites itself).
    "mutating": True,
    "function": {
        "name": "sandbox_fetch_reference",
        "description": (
            "Copy the BINARY of an uploaded reference (an image, an audio file, a "
            ".docx, a .zip) INTO your sandbox workspace so code here can read its "
            "bytes. "
            "Pass the reference's id (ref_id); the file lands at a deterministic "
            "path returned in the result — open it from there. The reverse of "
            "sandbox_path on import_file (node_type=\"reference\").\n"
            # The text of an audio/.docx reference is ALREADY in the document
            # content (the import pipeline produced it) — read_document it; do NOT
            # fetch the binary to re-transcribe or re-extract. Re-deriving text from
            # the binary is forbidden (no ASR / OCR / converters here); if the text
            # is wrong or empty, call reprocess_reference or ask the user. This line
            # is what steers the agent away from the incident it replaced.
            "This is for the BINARY only. The text of an audio or .docx reference "
            "is already in the document content — read_document it; never "
            "re-transcribe or re-extract it here. If the text is bad or empty, "
            "reprocess_reference it (or ask the user), do not rebuild it yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ref_id": {
                    "type": "string",
                    "description": "The reference id to copy into the workspace.",
                },
            },
            "required": ["ref_id"],
        },
    },
}

# ARCH: console-only like SANDBOX_FETCH_REFERENCE_TOOL. Read-only: it writes
# only the caller's own workspace cache (deterministic path, a re-fetch
# overwrites itself) — the same class as sandbox_fetch_reference.
SANDBOX_FETCH_SKILL_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "sandbox_fetch_skill",
        "description": (
            "Materialize a project skill's files (its SKILL.md plus every "
            "`scripts/…` and `references/…` child document) INTO your sandbox "
            "workspace at a deterministic directory, overwriting in place. Pass "
            "the skill's frontmatter name; the result names the directory — run "
            "the skill's scripts from there. Call it BEFORE sandbox_bash whenever "
            "a loaded skill lists a `## Files` section: the fetch alone restores "
            "the files, never rewrite them from memory or from the Spec."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill's frontmatter name (kebab-case).",
                },
            },
            "required": ["name"],
        },
    },
}

# ARCH: console-only like SANDBOX_FETCH_REFERENCE_TOOL. Read-only: it writes
# only the caller's own workspace cache (deterministic path, a re-fetch
# overwrites itself) — the same class as sandbox_fetch_skill.
SANDBOX_FETCH_ARTIFACT_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "sandbox_fetch_artifact",
        "description": (
            "Step 1 of rewriting an EXISTING html page in place. Writes the "
            "page's LIVE source to `artifacts/<document_id>/index.html` in your "
            "sandbox workspace (a re-fetch overwrites it). Then edit that file "
            "with sandbox_bash (sed, python, a heredoc — check it there too) "
            "and finish with replace_artifact_page on the same path. Use this "
            "route whenever the user asks to change an existing page beyond a "
            "few passages: a redesign, restructured markup, reworked script, "
            "new sections, or anything that would take more than 2-3 "
            "edit_document calls. For a small fix (one label, one value, one "
            "style rule) edit_document str_replace is enough."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "The html page document to fetch.",
                },
            },
            "required": ["document_id"],
        },
    },
}

# ARCH: the in-place rewrite route for an EXISTING html page — the agent-only
# alternative to creating a duplicate page. Whole-body replace through the
# single agent convergence path (route_document_content), checkpointed
# before the write so History restores the pre-replace page. Held for
# approval in confirmation mode like edit_document (the handler refuses a
# `confirm` call with the 409 ask signal).
REPLACE_ARTIFACT_PAGE_TOOL: dict = {
    "type": "function",
    "mutating": True,
    "function": {
        "name": "replace_artifact_page",
        "description": (
            "Step 2 of rewriting an EXISTING html page in place: replaces the "
            "page's WHOLE body with a file from your sandbox workspace "
            "(normally the one sandbox_fetch_artifact wrote and you edited). "
            "This is how an existing page gets a redesign, restructured "
            "markup, a reworked script or new sections — the user keeps the "
            "same page: same document_id, links to it, History and tree "
            "place. The previous body is checkpointed (History restores it); "
            "an unchanged file is a no-op. Choose by the size of the change: "
            "more than 2-3 passages → this route; a small fix → edit_document "
            "str_replace; a NEW page only when the user asks for a separate "
            "one → create_document. Markdown documents are refused — they are "
            "edited with edit_document."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "The html page document whose body is replaced.",
                },
                "sandbox_path": {
                    "type": "string",
                    "description": (
                        "The workspace path of the new page source (the file "
                        "sandbox_fetch_artifact wrote and you edited)."
                    ),
                },
                "apply": {
                    "type": "string",
                    "enum": ["confirm", "auto"],
                    "default": "confirm",
                    "description": (
                        "confirm (default) holds the replace for the user's "
                        "approval; auto applies it directly (requires full "
                        "access + opted-in)."
                    ),
                },
            },
            "required": ["document_id", "sandbox_path"],
        },
    },
}


# ARCH: defined OUTSIDE AGENT_TOOLS for the same
# reason as the SANDBOX_* tools — ComfyUI is an internal service an external MCP
# client cannot reach, so the tool must never appear in build_tool_list()'s public
# surface. Only agent_toolset (the agent path) appends it. The `prompt` the
# agent supplies is EMBELLISHED server-side into a full SD prompt; the
# description says so to keep the agent from hand-crafting SD syntax (it would be
# rewritten). Exempt from the per-target repeat bound: the random seed makes
# a repeat a legitimate "regenerate".
#
# WHY: the AGENT owns cross-turn continuity of a generated image — every
# call must carry a COMPLETE scene description, not a delta.
# Why: the server-side refiner sees only this
# `prompt` — it has no chat history, no previous seed, and no previous SD prompt
# (that one is patched into the ComfyUI graph and persisted nowhere). The agent
# is the only participant whose context holds its own prior generate_image calls,
# so a description inviting "make it darker" alone strands the rest of the scene.
GENERATE_IMAGE_TOOL: dict = {
    "type": "function",
    # mutating=True buys no proxy timeout in the dsh driver (a ComfyUI generation
    # is not cut at the read-tool bound), exactly like sandbox_bash. It does NOT drag in the
    # proposal/confirm path — the handler returns status:"applied" directly.
    "mutating": True,
    "function": {
        "name": "generate_image",
        "description": (
            "Generate an image via ComfyUI and attach it as an image reference to "
            "the working document. Pass the FULL description of the image you want "
            "as `prompt`; the server only rephrases it into stable-diffusion "
            "wording — it sees nothing else. Returns {status:'generating', "
            "run_id, doc_id} IMMEDIATELY — the image is produced asynchronously "
            "(~10–20s) and lands on the chat's working document on its own; do "
            "NOT wait for it or poll. Requires a working document (open one in "
            "this chat first). Re-calling with the same intent regenerates (the "
            "seed is randomized)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": (
                        "A COMPLETE, self-contained description of the image: "
                        "subject, setting, style, colors, lighting, notable "
                        "details. Plain prose, not SD tag syntax; the server "
                        "expands it."
                    ),
                },
                "document_id": {
                    "type": "string",
                    "description": "Host document id (the working document).",
                },
                "orientation": {
                    "type": "string",
                    "enum": ["square", "portrait", "landscape"],
                    "default": "square",
                    "description": (
                        "Output aspect ratio. square=1024x1024, "
                        "portrait=832x1216, landscape=1216x832."
                    ),
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 4,
                    "default": 1,
                    "description": (
                        "How many images to generate (1-4). Default 1. Omit for a "
                        "single image."
                    ),
                },
            },
            "required": ["prompt", "document_id"],
        },
    },
}

REPROCESS_REFERENCE_TOOL: dict = {
    "type": "function",
    # mutating=True buys no proxy timeout in the dsh driver and keeps it out of
    # the read-only line — exactly like the SANDBOX_* tools. It does NOT
    # drag in the proposal/confirm path on its own; the handler resolves that via
    # resolve_apply_mode (see routes/tool_api/imports.py).
    "mutating": True,
    "function": {
        "name": "reprocess_reference",
        "description": (
            "Re-run the import pipeline on a reference: an audio reference is "
            "re-transcribed, a .docx-backed markdown reference is re-converted. "
            "Use this when a reference's text is wrong or empty — NEVER rebuild "
            "the text yourself (no ASR / OCR / converters in the sandbox).\n"
            "It WIPES the current text and queues the job — the result is async "
            "(the worker writes it back later), so after it applies, summarize and "
            "end the turn (or re-read it and check processing_status). Under "
            "confirmation the call is held for the user's approval and does NOT wipe "
            "until approved — because the wipe is not History-recoverable for a "
            "reference.\n"
            "Image references are NOT reprocessable — there is no OCR; look at the "
            "image (it is in the conversation) or ask the user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reference_id": {
                    "type": "string",
                    "description": "The reference id to re-process.",
                },
                "apply": {
                    "type": "string",
                    "enum": ["confirm", "auto"],
                    "default": "confirm",
                    "description": (
                        "confirm (default) proposes the wipe for approval; auto "
                        "applies it directly (requires full access + opted-in)."
                    ),
                },
            },
            "required": ["reference_id"],
        },
    },
}

"""Media / sandbox / exec agent tool CONSTANTS — the agent-only surface that
reaches internal services an external MCP client cannot. Pure data — imports
nothing from the agent package (leaf).
"""

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
            "sandbox console, or `attachment_index` for an image attached to your "
            "current message. Applies immediately. For very large files prefer "
            "the upload widget.\n"
            # The create-vs-upload boundary
            # is "text I authored" → create_document (may propose); "bytes I am
            # importing" → import_file (applies immediately). Plain `content`
            # (authored text) is NOT an input here — use create_document for that,
            # passing `normalize: true` if the text is a .md file's body.
            # content_base64 is
            # NOT an input — you cannot see file bytes, so never emit base64 into
            # this call. Bytes arrive only via sandbox_path (a file you produced)
            # or attachment_index (an image the user attached).
            # To share a bundle of sandbox files (e.g. a built web page), pack it
            # with `python -m zipfile -c out.zip <paths…>` — the console has NO
            # zip binary, python's stdlib is the route — then import the .zip as
            # a reference; the user gets a downloadable archive.
            "Binary image/audio/archive is not a document — set "
            "node_type=\"reference\" for those."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": (
                        "Source filename. A .md/.markdown suffix on a text file runs "
                        "markdown normalize; the extension picks image vs audio for "
                        "binary, and .zip means a downloadable archive reference."
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
                        "attachment_index. This is the ONLY way to import a file you "
                        "produced — you cannot see file bytes, so never emit base64. "
                        "To share several files at once, pack them with "
                        "`python -m zipfile -c out.zip <paths…>` (no zip binary on "
                        "the console) and pass the .zip here."
                    ),
                },
                "attachment_index": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "INTERNAL (in-chat agent only): 0-based index of an image "
                        "attached to your current message, in order of appearance. "
                        "The driver resolves it to that image's bytes — use this to "
                        "import an attached image. Mutually exclusive with "
                        "sandbox_path."
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
    # mutating=True buys ONE property: sequential execution in the dsh driver (one
    # console, one command at a time). It does NOT drag in the proposal/confirm path
    # — that is resolved by the document endpoints via resolve_apply_mode, not by
    # membership here — and it is EXEMPT from the per-target repeat bound
    # (the turn telemetry's per-target repeat bound). Why the exemption: re-running `pytest` after a fix is
    # normal shell work, whereas an identical repeated document edit is a stuck loop.
    "mutating": True,
    "function": {
        "name": "sandbox_bash",
        "description": (
            "Run a bash command on your sandbox: a Linux host with Python, pip, gcc "
            "and git, plus pandas/numpy preinstalled. Use it to write and run "
            "scripts, do calculations, process data and install packages "
            "(`pip install X`) — it has internet access. It is a real console, so "
            "re-running a command after a fix is normal.\n"
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
            "document vs reference) — do not base64-encode it into the request.\n"
            # Detached mode: for a command that would
            # outlive the 300 s foreground ceiling (a test suite, a build). The
            # concurrency warning must be stated — detached runs make the workspace
            # concurrent for the first time, and the model is what decides not to
            # launch two builds over one checkout.
            "For a LONG command (a full test suite, a build) that would exceed the "
            "300-second foreground cap, pass `detach: true`: it starts the command "
            "in the background, returns `{status:\"running\", run_id}` at once, and "
            "the run keeps going after this call. Poll it with `sandbox_run_status` "
            "(read-only) and do NOT busy-wait in this turn — the per-turn tool-call "
            "ceiling will stop you; an unfinished run is collected in the NEXT turn. "
            "A detached run still dies at its own ceiling (hours) and is killed "
            "then. The workspace is ONE file tree shared with every other call, so "
            "do NOT launch two detached runs that would fight over the same "
            "checkout or files — the runs are not isolated.\n"
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
                    "description": "Seconds to wait before killing the command "
                                   "(default 60, max 300). Ignored when detach is true.",
                },
                "detach": {
                    "type": "boolean",
                    "description": (
                        "Start the command in the background and return "
                        "{status:\"running\", run_id} at once (default false). "
                        "For a command that must outlive the 300 s foreground cap. "
                        "The run is bounded by the detached ceiling, not `timeout`; "
                        "collect it with `sandbox_run_status` in the same or a "
                        "later turn."
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
    # read-only: polls a detached run's directory and touches nothing — outside
    # MUTATING_TOOLS by design, so polling does not take the console (sequential
    # execution) and several runs can be in flight at once.
    "mutating": False,
    "function": {
        "name": "sandbox_run_status",
        "description": (
            "Poll a DETACHED run started with `sandbox_bash(detach: true)`. "
            "Returns {status: running|exited|killed, run_id, exit_code, out, "
            "truncated}: `out` is a capped tail of the run's output, and "
            "`truncated: true` means the real log is longer. `killed` with "
            "exit_code 124/137 means the run hit its detached wall-clock ceiling.\n"
            # The busy-poll warning is load-bearing: the repeat guard reminds on
            # identical consecutive calls and the whole-turn wall-clock deadline
            # bounds a wait-here loop, so an unfinished run is collected in the
            # NEXT turn, not waited out in this one. Also names the missing-run
            # 404 so the model does not read it as a wedged sandbox.
            "Do NOT busy-wait on this in the current turn — the per-turn tool-call "
            "ceiling will stop you with the run still going. Poll a few times, and "
            "if it is still `running`, end your turn and collect it in the NEXT "
            "turn. A 404 means no such run (already GC'd or never started)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": (
                        "The run_id from `sandbox_bash(detach: true)`'s result "
                        "`{status:\"running\", run_id}`."
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
SANDBOX_FETCH_REFERENCE_TOOL: dict = {
    "type": "function",
    # mutating=True for the SAME property as sandbox_bash (sequential execution),
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
    # mutating=True buys sequential execution in the dsh driver (one ComfyUI
    # generation at a time), exactly like sandbox_bash. It does NOT drag in the
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
    # mutating=True buys ONE property — sequential execution in the dsh driver (one
    # content-wipe+queue at a time) — exactly like the SANDBOX_* tools. It does NOT
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

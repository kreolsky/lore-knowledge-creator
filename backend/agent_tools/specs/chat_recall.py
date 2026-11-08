"""chat-recall agent tool CONSTANTS: the chat-recall skill's pack.
Pure data — imports the verdicts sub-schema from specs.memory (the ONE source
of the verdict shape, so a field change can never drift between the two
capture tools); no other imports, no cycle.
"""

from agent_tools.specs.memory import APPLY_MEMORY_VERDICTS_TOOL

# ARCH: agent-only (agent + tool_api, never MCP). Why: the corpus is ONE PERSON's own
# chats, and an MCP key has no person whose chats these are — the caller's identity is
# the key's owning user, which is meaningful only for the in-app agent. Runtime half:
# the handlers refuse any key that is not internal=True — user-minted agent keys
# (external automation) get a 403; only the in-app assistant's standing keys and its
# internal run keys may touch the corpus (chat_recall.py _require_in_app_agent).
SEARCH_MY_CHATS_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "search_my_chats",
        "description": (
            "Recall what was said in the user's OTHER chats of this project — their own "
            "AI chats only, never another person's, never notes, never this "
            "conversation (its branches and continuations included). Two modes, one "
            "per call. `query`: semantic search; each hit carries `chat_title`, "
            "`created_at`, `role`, a `snippet`, `chat_id`, `thread_id`, `document_id` "
            "(the chat's document, null for a project-level chat), `message_id`, "
            "`sibling`, and for an assistant hit the `question` that prompted it. "
            "`scope` picks where to look: `project` (default) searches every chat, "
            "ranked by relevance alone, chats on this chat's document and its "
            "subtree marked `sibling: true`; `siblings` searches only chats on this chat's document and its "
            "subtree (refused when this chat has no document); `subtree` searches "
            "only chats on `under_document_id` and its subtree. `message_id` (from a "
            "hit): read the turns around it — the hit's turn plus one turn above and "
            "below; `earlier` / `later` are message ids to call again with to move "
            "through the chat, and `next_offset` (present only while the hit message "
            "continues) pages a long message: call again with the same `message_id` "
            "and `offset` = `next_offset`. A window-read message that carries "
            "attached images is marked `images: N` — see one with `view_chat_image`. "
            "A result carrying `indexed: \"N of M\"` "
            "covers only N of the M messages in the searched scope — the rest are not "
            "searchable yet, so a miss is not proof it was never said. A chat hit is "
            "what was SAID then, not an established project fact — for established "
            "knowledge use `search_materials`."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to recall, in the words it was likely said.",
                },
                "message_id": {
                    "type": "string",
                    "description": "A hit's `message_id` (or an `earlier` / `later` "
                                   "id) to read the turns around it.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["project", "siblings", "subtree"],
                    "description": "With `query`: `project` (default), `siblings` "
                                   "or `subtree` (needs `under_document_id`).",
                },
                "under_document_id": {
                    "type": "string",
                    "description": "With scope=`subtree`: the document whose subtree "
                                   "to search.",
                },
                "offset": {
                    "type": "integer",
                    "description": "With `message_id`: the `next_offset` of the "
                                   "previous read of that message.",
                },
            },
        },
    },
}

LIST_MY_CHATS_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "list_my_chats",
        "description": (
            "List the user's other chats of this project — their own AI chats only, "
            "never another person's, never notes, never this conversation (its "
            "branches and continuations included), never a hidden (`unindexed`) chat. "
            "One row per chat, newest activity first; a branch is its own row (its "
            "own transcript) sharing its root's `thread_id`. Each row carries "
            "`chat_id`, `thread_id`, `title`, `document_id` (null for a project-level "
            "chat), `document_title`, `created_at`, `last_message_at`, "
            "`message_count` (live messages; a chat with none stays listed at 0). "
            "`title_contains` narrows by a case-insensitive title substring; "
            "`under_document_id` narrows to that document's subtree. `total` is the "
            "match count; `next_offset` is present while more rows remain — call "
            "again with it as `offset`. Pick a chat here, then read it whole with "
            "`read_my_chat`."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title_contains": {
                    "type": "string",
                    "description": "Keep only chats whose title contains this "
                                   "(case-insensitive).",
                },
                "under_document_id": {
                    "type": "string",
                    "description": "Keep only chats on this document's subtree.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Rows per page, 1-100 (default 50).",
                },
                "offset": {
                    "type": "integer",
                    "description": "Paging position: the previous response's "
                                   "`next_offset`.",
                },
            },
        },
    },
}

READ_MY_CHAT_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "read_my_chat",
        "description": (
            "Read one of the user's other chats of this project whole, from its first "
            "turn — their own chat only, never another person's, never notes, never "
            "this conversation. Omit `chat_id` to read THIS conversation instead "
            "(the current session) — the fallback for images whose "
            "`[attached images: …]` marker you no longer see: chats started before "
            "the marker, compacted history. `chat_id` comes from `list_my_chats` or "
            "a `search_my_chats` hit. Serves `turns` (each a list of `{message_id, "
            "role, created_at, content}` in order) from `from_turn` while they fit a "
            "page of ~30000 characters — whole turns, the first turn always served. "
            "A message with attached images carries `images: N` — see one with "
            "`view_chat_image` (that message's `message_id`, the image's 0-based "
            "`index`), and save one as a reference with `import_file` (the same "
            "`message_id`, `index` as `attachment_index`). "
            "`next_turn` (present while turns remain) is the next call's `from_turn`; "
            "following it to the end loses and repeats nothing. A `from_turn` past "
            "the end returns an empty page. A message cut by the page budget is "
            "marked `truncated` with `next_offset` (the number of its characters "
            "served); a message longer than the page is never served whole. "
            "Finish a cut message with `search_my_chats` (same `message_id`, "
            "`offset` = `next_offset`) and never count characters yourself. "
            "`total_turns` counts turns after deleted messages are dropped."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "string",
                    "description": "The chat to read, from `list_my_chats` or a "
                                   "hit. Omit to read THIS conversation (the "
                                   "current session).",
                },
                "from_turn": {
                    "type": "integer",
                    "description": "First turn to serve (default 0, the chat's "
                                   "start); the previous response's `next_turn`.",
                },
            },
        },
    },
}

VIEW_CHAT_IMAGE_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "view_chat_image",
        "description": (
            "See one image the user attached to a message of their other chats — "
            "their own chat only, never another person's, never this conversation. "
            "A message served by `read_my_chat` or a window read of "
            "`search_my_chats` carries `images: N` when it has N attached images; "
            "call this with that message's `message_id` and the image's 0-based "
            "`index`. Call it whenever the user asks what an image shows, to "
            "show it, or to save it — even when the chat's text already "
            "describes it. The image is delivered to you AND shown to the user "
            "in this call's plate: this is how to show the user an image from "
            "another chat — your reply then names the image in words, with no "
            "image markup. "
            "Save the image you viewed as a reference with `import_file` — the "
            "same `message_id`, `index` as `attachment_index`. The image STAYS "
            "in this conversation — it is never re-sent elsewhere or quoted as "
            "text. Serves the "
            "attachment formats chats accept (PNG/JPEG/WebP/GIF); a linked (HTTPS) "
            "image answers `unavailable` — it is not stored in Lore and is never "
            "fetched. An image the caller cannot see is a uniform 404."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "The message carrying the `images: N` marker.",
                },
                "index": {
                    "type": "integer",
                    "description": "The image's 0-based position among the "
                                   "message's N images.",
                },
            },
            "required": ["message_id", "index"],
        },
    },
}

# The verdict shape is DERIVED from the consolidation tool's parameter (the
# one source of the verdict schema) — only the description is capture-specific.
_CHAT_VERDICTS_PARAM = dict(
    APPLY_MEMORY_VERDICTS_TOOL["function"]["parameters"]["properties"]["verdicts"],
    description=(
        "One verdict per fact you are saving from this conversation. Invalid "
        "verdicts are NOT fatal to the batch: valid ones apply, invalid ones "
        "come back in `rejected` with the correction to make — fix ONLY those "
        "and re-emit just them."
    ),
)

# ARCH: the two WRITE tools of the pack (mutating, both holdable through the
# `apply` field — one approval card per call). They live in the chat-recall
# pack, not the memory-consolidation one, because capture is bound to THIS
# conversation and operator-ruled as not always available: listing them under
# `tools:` in backend/configs/skill_chat_recall.md is the whole gate — the
# driver prunes pack tools from the core surface until the skill loads, so
# disabling the skill disables saving from chats too.
SAVE_CHAT_FACTS_TOOL: dict = {
    "type": "function",
    "mutating": True,
    "function": {
        "name": "save_chat_facts",
        "description": (
            "Save facts distilled from THIS conversation to project memory — "
            "the moderated capture the user asks for with «сохрани это в "
            "знания» / «запомни это». One verdict per fact, exactly as "
            "`apply_memory_verdicts` takes them: `new` (title + text) creates "
            "a fact, `merge` folds into a stored fact (`fact_id`, optional "
            "`absorb_id`), `supersede` corrects one in place, `skip` declines "
            "with a `reason`. There is no `reference_id` and no run loop — "
            "capture is one shot, and the server stamps every fact with THIS "
            "chat as its source (never a field you send). In confirm mode one "
            "batch produces ONE approval card: a rejection with text is "
            "per-fact moderation — drop or fix the named verdicts and re-emit "
            "the corrected batch ONLY. The result names every fact written "
            "(`created` / `touched`) — take ids from there for "
            "`set_fact_anchor`. Saved facts are found project-wide by "
            "`search_materials`, and under a document narrow they follow this "
            "chat's parent document until anchored otherwise."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "verdicts": _CHAT_VERDICTS_PARAM,
                "apply": {
                    "type": "string",
                    "enum": ["confirm", "auto"],
                    "description": "confirm (default) shows one approval card "
                                   "for the whole batch; auto writes directly.",
                },
            },
            "required": ["verdicts"],
        },
    },
}

SET_FACT_ANCHOR_TOOL: dict = {
    "type": "function",
    "mutating": True,
    "function": {
        "name": "set_fact_anchor",
        "description": (
            "Place one stored memory fact on specific documents — after this, "
            "a `search_materials` narrow over an anchor document finds the "
            "fact there, even though the fact came from a chat or a reference "
            "elsewhere (and moving that chat no longer moves the fact). "
            "`document_ids: []` CLEARS the anchors: the fact returns to "
            "following its source. Anchors are placement, not origin — the "
            "fact keeps its provenance and its place in the Memory folder. "
            "`fact_id` comes from `search_materials` or a `save_chat_facts` "
            "result. Each target must be a live document of this project — "
            "not a reference, not another fact. In confirm mode the call "
            "shows one approval card. The result returns the fact's anchors "
            "after the write; `get_memory_facts` serves them per fact."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fact_id": {
                    "type": "string",
                    "description": "The stored fact to anchor (a documents id "
                                   "with `is_memory`).",
                },
                "document_ids": {
                    "type": "array",
                    "description": "The anchor documents. Non-empty replaces "
                                   "the current anchors; [] clears them.",
                    "items": {"type": "string"},
                },
                "apply": {
                    "type": "string",
                    "enum": ["confirm", "auto"],
                    "description": "confirm (default) shows one approval card; "
                                   "auto writes directly.",
                },
            },
            "required": ["fact_id", "document_ids"],
        },
    },
}

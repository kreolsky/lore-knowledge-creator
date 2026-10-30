"""chat-recall agent tool CONSTANTS: the chat-recall skill's pack.
Pure data — imports nothing (leaf).
"""

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

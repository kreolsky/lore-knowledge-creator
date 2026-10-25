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
            "and `offset` = `next_offset`. A result carrying `indexed: \"N of M\"` "
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
            "this conversation. `chat_id` comes from `list_my_chats` or a "
            "`search_my_chats` hit. Serves `turns` (each a list of `{message_id, "
            "role, created_at, content}` in order) from `from_turn` while they fit a "
            "page of ~30000 characters — whole turns, the first turn always served. "
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
                    "description": "The chat to read, from `list_my_chats` or a hit.",
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

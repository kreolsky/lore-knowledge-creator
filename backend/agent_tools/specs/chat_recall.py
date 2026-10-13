"""chat-recall agent tool CONSTANT: `search_my_chats`, the chat-recall skill's pack.
Pure data — imports nothing (leaf).
"""

# ARCH: agent-only (agent + tool_api, never MCP). Why: the corpus is ONE PERSON's own
# chats, and an MCP key has no person whose chats these are — the caller's identity is
# the key's owning user, which is meaningful only for the in-app agent.
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
            "with chats on this chat's document and its subtree first (`sibling: "
            "true`); `siblings` searches only chats on this chat's document and its "
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

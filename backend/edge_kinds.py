"""The edge-kind vocabulary — ONE source for every kind list (plan table-links-notes).

Stored kinds are rebuilt into doc_mentions by mentions.py (kind stamped on the
edge); projected kinds are computed at READ time — `attachment`/`note`/
`chat-context` from indexed fields (edges.py), `external` from the GFM
read-model text (mentions.extract_external_links). Nothing but the stored pair
ever reaches the DB, so no writer can drift the graph.

Why a leaf module: the pydantic Literal on ToolGetLinks.kinds (models/tools.py)
and the advertised enum in agent_tools/specs/read.py must BOTH derive from
ALL_EDGE_KINDS, but models/tools.py cannot import edges.py (models/__init__
imports tools, edges imports mentions, mentions imports models — the cycle
breaks on import order). Every kind list in the tree derives from these tuples.
"""
from __future__ import annotations

KIND_MENTION = "mention"
KIND_TRANSCLUSION = "transclusion"
KIND_ATTACHMENT = "attachment"
KIND_NOTE = "note"
KIND_CHAT_CONTEXT = "chat-context"
# Content-parsed, never stored: the /edges + get_links projection reads the GFM
# read-model (live session expand_tables, else documents.content) — the exact
# text mention edges parse.
KIND_EXTERNAL = "external"

# The kinds rebuilt into doc_mentions by mentions.rebuild_doc_mentions.
STORED_EDGE_KINDS = (KIND_MENTION, KIND_TRANSCLUSION)

# One default for both surfaces (route + agent tool): the content-derived kinds
# plus attachments; note/chat-context/external are opt-in (program decisions:
# chat-context is private/per-user, notes are a comment surface, and external
# must not change the agent get_links default output).
DEFAULT_EDGE_KINDS: tuple[str, ...] = (
    KIND_MENTION, KIND_TRANSCLUSION, KIND_ATTACHMENT,
)
ALL_EDGE_KINDS: tuple[str, ...] = (
    *STORED_EDGE_KINDS, KIND_ATTACHMENT, KIND_NOTE, KIND_CHAT_CONTEXT,
    KIND_EXTERNAL,
)

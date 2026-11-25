"""Read-side agent tool CONSTANTS: the search/read/orient entries of
AGENT_TOOLS (READ_TOOLS) plus the two project-memory readers served only on
the agent path (GET_MEMORY_FACTS_TOOL, GET_FACT_HISTORY_TOOL). Pure data —
imports nothing from the agent package (leaf).
"""
from edge_kinds import ALL_EDGE_KINDS

from agent_tools.specs.subtree import EXPORT_SUBTREE_MD_TOOL

READ_TOOLS: list[dict] = [
    {
        "type": "function",
        "mutating": False,
        "function": {
            "name": "search_materials",
            # WHY: the source order is conversation context > search_materials > web_search,
            # and a well-known subject is not an exception to it.
            # Why: separating the two tools by TOPIC ("outside" vs "inside") left the tie
            # unresolved, and the agent reached for the internet on generally-known material the
            # project had its own position on — an external snippet then silently outranked the
            # project's established knowledge. Stated on both surfaces Lore owns: this
            # description and configs/skill_web_search.md (web_search itself is dsh's tool).
            "description": (
                "Semantic search over this project's documents, references and memory. "
                "The project outranks the internet: search here before `web_search`, "
                "even on well-known subjects — the project may hold its own position. "
                "Only material already in this conversation outranks it.\n"
                "Hits carry `kind`: `memory` = a distilled fact the project established "
                "(prefer it over raw material), with `sources` = the reference ids it "
                "came from; `document` / `reference` = a raw snippet, the first hit per "
                "document with its `outline` — judge from it whether to open the "
                "document. "
                # WHY: the route to `sources` is triggered by an answer the model
                # presents as detailed or grounded, NOT only by a verbatim quote.
                # Why: gated on quoting alone, a model reads "no quote requested" as
                # "sources not needed" and answers a detailed question from distilled
                # snippets — a memory hit is a claim, never the evidence for it.
                "Snippets are not content: to quote verbatim, or for any answer you "
                "present as detailed or grounded in the project, call `read_document` "
                "on the `sources` ids and read the WHOLE reference — never search for "
                "the passage. "
                # `corpus` names WHAT to search in the project's entity model:
                # references are the raw working stock, documents the compiled
                # artifact, memory the distilled knowledge (each fact carrying
                # `sources` back into the raw stock). The two real needs are
                # named positively — `all` (default) and `memory` (only the
                # consolidated knowledge, noise already gone); `documents` /
                # `references` are the rare raw-kind narrows. `corpus` narrows
                # the search, never the read: read_document works on any id
                # under any value, so from a `corpus: "memory"` hit the
                # `sources` → read_document cycle stays usable.
                # The VALUE prose lives in the property description (beside the
                # enum it explains) — search_materials is the most expensive
                # schema in the set, and a tool-level duplicate of the same
                # sentences cost ~350 chars of per-turn context. Do not mirror
                # it back here.
                "\n"
                # WHY: an answer keeps project material and the model's own
                # knowledge labelled apart, and states what the project does not cover.
                # Why: the same rule as no-silent-degradation — never present one thing
                # as another. Observed: general medical claims (contraindications,
                # bleeding symptoms) blended unlabelled into an answer framed as what
                # the project documents, which is how a reader ends up trusting the
                # model's weights as this project's established position.
                "Keep apart what the project's material states and what you add from "
                "your own knowledge: label the latter as yours, and say plainly what "
                "the project does not cover.\n"
                "Write `query` as the answer would read (the statement or key terms), "
                "not as a question; word forms are handled — no inflection or synonym "
                "lists. No operators: quotes, `*` and filenames are plain text. For a "
                "literal string (id, filename, code) use `mode: exact` — an empty "
                "exact result means the string is not in the project. Thin result: "
                "raise `k` or search for something else; rewording the same search "
                "wastes a turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for."},
                    "intent": {
                        "type": "string",
                        "description": (
                            "Short human phrase for what you look for; shown to the "
                            "user instead of `query`, not searched."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["semantic", "exact"],
                        "default": "semantic",
                        "description": (
                            "`semantic` (default): meaning + word forms + literal "
                            "substrings. `exact`: the literal `query` only — a strict "
                            "subset of `semantic`, never more."
                        ),
                    },
                    "under_document_id": {
                        "type": "string",
                        "description": (
                            "Optional: restrict to this document's subtree (itself + "
                            "descendants). Memory is then kept only if placed there "
                            "(from a reference hosted in it, a chat parented there, "
                            "or anchored to a document in it) — fewer memory hits is "
                            "not absence of knowledge; re-run without it for all "
                            "memory. An unknown or out-of-scope id is an error, not "
                            "an empty result."
                        ),
                    },
                    "corpus": {
                        "type": "string",
                        "enum": ["all", "memory", "documents", "references"],
                        "default": "all",
                        "description": (
                            "`all` (default): documents + references + memory. "
                            "`memory`: only distilled facts. `documents` / "
                            "`references`: only that raw kind. Narrows the search, "
                            "never read_document."
                        ),
                    },
                    "k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "mutating": False,
        "function": {
            "name": "read_document",
            "description": (
                "Read a document or reference by `document_id` — a title is not an "
                "address; ids come from search_materials or get_project_structure. "
                "`content` is the raw editor buffer: always read before edit_document "
                "so old_string is byte-verbatim. Also returns `media_type`, `has_file`, "
                "`processing_status` (`queued` | `processing` | `ready` | `error` | "
                "null; the last three are terminal, null = no derived text expected, "
                "not a missing file), `tables[]` and `references[]` (id, title, "
                "media_type, processing_status, has_file, created_at — titles repeat, "
                "`created_at` tells them apart). LONG CONTENT IS SLICED: re-call with "
                "`offset` = `next_offset` until it is absent — only then do you hold "
                "the whole document. `empty: true` = the document has no text; the "
                "read is final, do not re-read it. A sliced read lists `sections` (every heading "
                "with its offset) to jump straight to one. Tables sit in `content` as "
                "![label](table:id) anchors — never edit_document across them, use "
                "edit_table_cell; `tables` indexes the whole document even when sliced. "
                "An image reference (`media_type: image`, `has_file: true`) can be "
                "seen with view_reference_image."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "string",
                        "description": "Document or reference id.",
                    },
                    # read_table folded in.
                    # Default "index" returns table_id+label+n_cols only (cheap); a
                    # weak agent otherwise gets every full grid dumped into context.
                    "tables": {
                        "type": "string",
                        "enum": ["index", "inline", "none"],
                        "default": "index",
                        "description": (
                            "`index` (default): table_id + label + n_cols. `inline`: "
                            "every full grid. `none`: omit."
                        ),
                    },
                    "table_id": {
                        "type": "string",
                        "description": (
                            "Optional: return only this table's grid. Without `limit` "
                            "`content` is empty — fetch a table this way instead of "
                            "re-reading the document."
                        ),
                    },
                    # Read-path spill bound: the content slice window.
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        # Paging (next_offset, total_chars) is stated once, in the
                        # tool description — not re-explained per parameter.
                        "description": "Code-point start of the content slice. Default 0.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Max characters from offset; unset = default bound.",
                    },
                    "version": {
                        "type": "string",
                        "description": (
                            "Optional `checkpoint_id` from list_document_versions: read "
                            "that past version — read-only GFM, tables inlined, no "
                            "`tables`/`table_id`, never an edit_document source."
                        ),
                    },
                },
                "required": ["document_id"],
            },
        },
    },
    {
        "type": "function",
        "mutating": False,
        "function": {
            "name": "get_project_structure",
            "description": (
                "Flat tree rows (id, parent_id, title, flags), no content. Without "
                "start_id: an ORIENTATION map — 2 layers, references omitted, the "
                "agent-config and Memory subtrees as door rows; whatever is withheld "
                "is counted on its parent row (child_count, n_references, "
                "subtree_total, subtree_depth) — drill into a branch with start_id. "
                "With start_id: that node's whole subtree, references and system "
                "contents included; `include_outline` adds each document row's "
                "headings (' · '-separated; outline_hidden = headings that did not "
                "fit). Not a substitute for search_materials."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_id": {
                        "type": "string",
                        "description": (
                            "Optional: enumerate this document's whole subtree. "
                            "Omitted = the root orientation map; under a "
                            "subtree-scoped key, the key's whole subtree."
                        ),
                    },
                    "depth": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "description": (
                            "Optional max layers: default 2 on a root call, the "
                            "whole subtree on a start_id call."
                        ),
                    },
                    "include_references": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Root call only: list reference rows (media_type, "
                            "source_url). Default: counted as n_references on the "
                            "host row. start_id calls always list them."
                        ),
                    },
                    "include_outline": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "start_id calls only (422 on a root call; a "
                            "subtree-scoped key needs no start_id): add H1–H4 "
                            "outlines to document rows."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "mutating": False,
        "function": {
            "name": "get_links",
            "description": (
                "Typed links of one document or reference, incoming and outgoing — "
                "exactly what the user's links panel shows. Kinds: `mention` "
                "(`[t](target)`), `transclusion` (`![t](target)`), `attachment` (a "
                "material attached to it), `note` (a pinned note: session_id + label "
                "instead of document_id), `external` (outgoing http(s) links, incl. "
                "table cells: url + label). Document peers carry document_id, title, "
                "is_reference, source_type, updated_at, and `broken` on outgoing "
                "mention/transclusion whose target is deleted. Peers outside the "
                "project or your key's scope are not served."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {
                        "type": "string",
                        "description": "Document or reference id.",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["in", "out", "both"],
                        "default": "both",
                        "description": "in = links here, out = links from it.",
                    },
                    "kinds": {
                        "type": "array",
                        # DERIVED from edge_kinds.ALL_EDGE_KINDS — the same tuple
                        # the /edges route and the ToolGetLinks Literal validate
                        # against, so no surface can drift from the service.
                        "items": {"type": "string", "enum": list(ALL_EDGE_KINDS)},
                        "description": (
                            "Default: mention, transclusion, attachment — ask for "
                            "note / external explicitly."
                        ),
                    },
                },
                "required": ["document_id"],
            },
        },
    },
    EXPORT_SUBTREE_MD_TOOL,
    {
        "type": "function",
        "mutating": False,
        "function": {
            "name": "list_document_versions",
            "description": (
                "List a document's saved versions (snapshots taken before agent edits, "
                "before restores, at session ends, on large deletions, and named "
                "versions people saved), newest first. Each row: `checkpoint_id`, "
                "`created_at`, `kind` (`agent-auto`, `auto-backup`, `safety-open`, "
                "`editor-handoff`, `last-session`, `_backup` = state before the last "
                "restore, or `manual` with its `label`), `author`, `comment`, `chars`, "
                "and `lines_added` / `lines_removed` versus the next-older version "
                "(null for the oldest; a row whose stored content is unreadable "
                "carries `content_unavailable: true` and null stats). Old automatic "
                "snapshots are thinned over time. "
                "Page with `before_checkpoint_id` = the returned "
                "`next_before_checkpoint_id`; absent means no older versions. Read a "
                "version with read_document(version=...), compare with "
                "diff_document_versions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "limit": {
                        "type": "integer", "minimum": 1, "maximum": 50, "default": 20,
                    },
                    "before_checkpoint_id": {
                        "type": "string",
                        "description": "Return only versions older than this one.",
                    },
                },
                "required": ["document_id"],
            },
        },
    },
    {
        "type": "function",
        "mutating": False,
        "function": {
            "name": "diff_document_versions",
            "description": (
                "Unified diff (GFM, tables inlined, 3 context lines) of a document "
                "from one saved version to another, or to the current document "
                "(`to` = \"current\", the default). Returns `diff`, `identical`, and "
                "`lines_added` / `lines_removed` for the whole change; a long diff is "
                "cut with `truncated: true` while the counts stay complete."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "from_checkpoint_id": {"type": "string"},
                    "to": {
                        "type": "string",
                        "default": "current",
                        "description": "\"current\" or a checkpoint_id of this document.",
                    },
                },
                "required": ["document_id", "from_checkpoint_id"],
            },
        },
    },
]


# Agent-only and always served: the pixels of an image reference ride the image-result
# envelope, which only the dsh plugin consumes — not an MCP shape.
VIEW_REFERENCE_IMAGE_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "view_reference_image",
        "description": (
            "See an image reference's picture itself, not only its derived text. Use it "
            "whenever the user asks what an image shows, or when the image matters for "
            "the task. `document_id` comes from read_document's `references[]`, "
            "search_materials or get_project_structure; an image embedded in a document "
            "is a reference too — take its id from the `![](…/files/<id>/…)` link."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "Id of an image reference (media_type: image).",
                },
            },
            "required": ["document_id"],
        },
    },
}

# Agent-only, same posture as the other memory tools. A read: the fact bodies are served
# because the payload index deliberately carries none.
GET_MEMORY_FACTS_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "get_memory_facts",
        "description": (
            "Read the BODIES of specific facts — the on-demand half of the two-channel "
            "delivery. The consolidation payload's `memory_index` names every fact but "
            "carries no body; call this for the facts you are actually about to "
            "adjudicate (a title in the index you might merge against). Returns each "
            "fact with its body (the fact text) and revision marker, plus the ids that "
            "resolved to nothing. When the total payload would exceed the char "
            "ceiling, whole facts are deferred — they come back in `deferred` named by "
            "title; re-call with just those ids to read them. Never judge a merge "
            "against a partial read."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Fact document ids from the memory_index.",
                },
            },
            "required": ["ids"],
        },
    },
}

# Agent-only, same posture as the other memory tools (it belongs to the same
# credential-bearing loop). A read: the served payload carries a revision marker (a
# count); this returns the retired wording the marker stands in for.
GET_FACT_HISTORY_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "get_fact_history",
        "description": (
            "Read the VERSION HISTORY of one fact — the explicit-request reader for a "
            "revision. A served fact that carries `revisions: N` was superseded N "
            "times; this returns the retired wording + each predecessor's supersede "
            "reason + provenance, newest predecessor first. Call it ONLY for a fact "
            "whose `revisions` is non-zero, when the change matters. `fact_id` is the "
            "LIVE fact you currently see (from get_memory_facts) — a retired id is "
            "a 404."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fact_id": {
                    "type": "string",
                    "description": (
                        "The LIVE fact whose history you want — the one carrying a "
                        "non-zero `revisions` marker."
                    ),
                },
            },
            "required": ["fact_id"],
        },
    },
}

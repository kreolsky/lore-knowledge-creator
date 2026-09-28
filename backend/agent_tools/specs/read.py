"""Read-side agent tool CONSTANTS: the search/read/orient entries of
AGENT_TOOLS (READ_TOOLS) plus the two project-memory readers served only on
the agent path (GET_MEMORY_FACTS_TOOL, GET_FACT_HISTORY_TOOL). Pure data —
imports nothing from the agent package (leaf).
"""

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
                "Semantic search across documents, references and project memory. "
                "The project outranks the internet: answer from what this project "
                "holds before considering `web_search`, including on well-known "
                "subjects — the project may have its own established position. Only "
                "material already served in this conversation outranks this tool. "
                "Returns snippets + ids; use read_document to read the content "
                "(a long document arrives in slices — follow `next_offset` until "
                "it is absent). Each hit "
                "carries a `kind`: `memory` hits are distilled facts the project "
                "already established (served whole, prefer them over raw material); "
                "`document` / `reference` hits are raw excerpts. A `memory` hit also "
                "carries `sources` — the reference ids the fact was distilled from. "
                # WHY: the route to `sources` is triggered by an answer the model
                # presents as detailed or grounded, NOT only by a verbatim quote.
                # Why: gated on quoting alone, a model reads "no quote requested" as
                # "sources not needed" and answers a detailed question from distilled
                # snippets — a memory hit is a claim, never the evidence for it.
                "To quote the underlying passage verbatim, or to give any answer you "
                "present as detailed or grounded in this project, take a `sources` id "
                "and call `read_document` on it, following `next_offset` until "
                "absent to cover the WHOLE reference; "
                "do NOT search for the passage — search returns snippets, which cannot "
                "satisfy a verbatim quote, and guessing the wording loops. "
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
                "Keep the two sources of an answer apart: what this project's material "
                "states, and what you add from your own knowledge. Mark the second as "
                "your own knowledge instead of letting it read as the project's "
                "position, and say plainly what the project does not cover — an "
                "unstated gap filled from your own knowledge reaches the reader as an "
                "established project fact.\n"
                "\n"
                "Phrase `query` the way an ANSWER would be written, not the way a "
                "question is asked: the statement or the key terms you expect to find "
                "in the text. Word forms are handled for you — do not list inflections "
                "or synonyms by hand. Put the human version of what you want in "
                "`intent`; that is what the user sees, and it is not searched.\n"
                "\n"
                "This search has no query operators: quotes do not force an exact "
                "phrase, `*` is not a wildcard, and naming a file does not restrict "
                "the search to it — all three are matched as ordinary text. To match a "
                "literal string (an id, a filename, a code, an exact term) use "
                "`mode: \"exact\"`. `exact` can never find anything `semantic` would "
                "miss — it only removes matches, so an empty `exact` result means the "
                "string is not in the project, and re-running it in other words cannot "
                "change that.\n"
                "\n"
                "If a search comes back thin, raise `k` or search for a different "
                "thing — re-running the same search in other words is the one move "
                "that reliably wastes a turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for."},
                    "intent": {
                        "type": "string",
                        "description": (
                            "One short human-readable phrase naming what you are "
                            "looking for. Shown to the user in place of `query`. Not "
                            "searched."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["semantic", "exact"],
                        "default": "semantic",
                        "description": (
                            "`semantic` (default) searches by meaning + word forms + "
                            "literal substrings, fused by rank. `exact` matches the "
                            "literal `query` string only (a filename, an id, a code) "
                            "and is a strict subset of `semantic` — it never finds "
                            "more, only less."
                        ),
                    },
                    "under_document_id": {
                        "type": "string",
                        "description": (
                            "Optional: a document id whose subtree (itself + all "
                            "descendants) to restrict the search to. Default is the "
                            "whole project — narrowing is opt-in. When set, memory "
                            "is narrowed by provenance: a distilled fact is kept "
                            "iff it came from a reference hosted in the subtree, so "
                            "expect FEWER memory hits under a narrow (facts "
                            "recorded without source references drop too — a "
                            "known limitation, not absence of knowledge; re-run "
                            "without under_document_id for the full memory). An "
                            "unknown id, or one outside your key's subtree, is an "
                            "error (naming the remedy) — not an empty result. "
                            "This narrows WHERE the search runs; it does not "
                            "change `mode`."
                        ),
                    },
                    "corpus": {
                        "type": "string",
                        "enum": ["all", "memory", "documents", "references"],
                        "default": "all",
                        "description": (
                            "Which kind of material to search. `all` (default): "
                            "documents + references + memory. `memory`: only the "
                            "project's distilled facts — the consolidated "
                            "knowledge, noise already gone. `documents` / "
                            "`references`: only that raw kind. Narrows the "
                            "search, never the read — read_document accepts any "
                            "id under any value."
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
                "Read a document's text, plus `media_type`, `has_file`, "
                "`processing_status` (`queued` | `processing` | `ready` | `error` | "
                "null — `ready`, `error` and null are terminal; null means no derived "
                "text is expected for this node, NOT that the file is missing: an "
                "image has a file and a null status), `tables[]` and `references[]` "
                "(attached nodes: id, title, media_type, processing_status, "
                "has_file, created_at — titles are not unique, so use `created_at` "
                "to tell same-named nodes apart). "
                "Returned as the raw editor buffer (the same projection "
                "edit_document matches against) — always call before edit_document "
                "so old_string is byte-verbatim. LONG CONTENT IS SLICED: `content` "
                "carries at most `limit` characters from `offset`, with "
                "`total_chars` and — while content remains — `next_offset`; re-call "
                "with `offset` = `next_offset` to page. `next_offset` ABSENT means "
                "you hold the whole document, so never conclude it ends where a "
                "slice ends. Table blocks appear in `content` as opaque anchors like "
                "![label](table:id) — do NOT edit_document across them; `tables` "
                "indexes them (derived from the WHOLE document even when `content` "
                "is sliced) and edit_table_cell edits a cell. "
                "Requires `document_id` — a title is not an address; ids come from "
                "`search_materials` or `get_project_structure` (whose root call is a "
                "bounded map — drill into branches via the row counters)."
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
                            "How to return table blocks. \"index\" (default): a cheap "
                            "list of table_id + label + n_cols (no rows). \"inline\": "
                            "every table's full grid. \"none\": omit the tables field."
                        ),
                    },
                    "table_id": {
                        "type": "string",
                        "description": (
                            "Optional: return ONLY this table's full grid (overrides "
                            "tables mode). Use the table_id from the index."
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
                        "description": (
                            "Max characters to return from offset. Unset uses the "
                            "default bound."
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
                "A flat map of tree rows (id, parent_id, title, flags) — no "
                "content. A call WITHOUT start_id is ORIENTATION: two layers by "
                "default, references omitted, and the agent-config + Memory "
                "subtrees shown only as door rows. Everything withheld is "
                "COUNTED on its parent row (child_count, n_references, "
                "subtree_total, subtree_depth) — read the counters and drill "
                "into a branch with start_id. A call WITH start_id is "
                "ENUMERATION: that node's whole subtree, references and system "
                "contents included — pass include_outline there to add each "
                "non-reference row's outline (raw headings, flat and "
                "' · '-separated) so siblings are tellable apart without "
                "opening them; outline_hidden counts the headings that did not "
                "fit. Not a substitute for search_materials."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_id": {
                        "type": "string",
                        "description": (
                            "Optional: a document id whose subtree to enumerate "
                            "(itself + all descendants, references included). "
                            "With it the default breadth is the WHOLE subtree — "
                            "no depth default applies. If omitted, the root "
                            "orientation map is returned instead."
                        ),
                    },
                    "depth": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "description": (
                            "Optional: max layers. Defaults to 2 on a root "
                            "call (no start_id); on a start_id call it applies "
                            "only when passed — the whole subtree otherwise. "
                            "Caps at 10."
                        ),
                    },
                    "include_references": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Optional, root call only: list reference rows "
                            "(they carry media_type/source_url). Default false "
                            "— hidden references are counted as n_references "
                            "on the host row instead. A start_id call always "
                            "lists references."
                        ),
                    },
                    "include_outline": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Optional, start_id calls only: add an outline "
                            "(raw H1–H4 headings, cleaned and budgeted) to "
                            "non-reference rows. Refused with 422 on a root "
                            "call — the orientation map stays layered; "
                            "outlines exist on enumeration only."
                        ),
                    },
                },
            },
        },
    },
]


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

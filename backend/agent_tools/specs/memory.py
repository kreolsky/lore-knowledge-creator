"""Project-memory agent tool CONSTANTS: the consolidation loop surface
(consolidate_memory / next_reference / apply_memory_verdicts). Pure data —
imports only `config` (leaf).
"""
from config import MEMORY_VERDICTS_MAX_PER_BATCH

# ARCH: CONSOLIDATE_MEMORY_TOOL is agent-only — appended in
# agent_toolset(), never added to AGENT_TOOLS (which build_tool_list turns into the
# public MCP surface). Same posture as SANDBOX_* / GENERATE_IMAGE / REPROCESS.
# Why, and it is NOT the usual reason: this tool has no external dependency and does
# not mutate a document. It MINTS A CREDENTIAL — a fresh agent key sandboxed to the
# project's Memory folder, which is the run's identity and the wall its writes run
# behind. Advertising a credential-minting call to arbitrary external MCP clients is
# a different risk class from advertising a read, and the MCP surface is force-auto
# with no confirmation tier to catch it. Personas are an agent-side concept, so "callable
# under any persona" (design P13) is fully satisfied here; MCP is not what that
# clause was about.
CONSOLIDATE_MEMORY_TOOL: dict = {
    "type": "function",
    # Non-mutating: this BUILDS the task payload for a consolidation run. No document
    # changes — the writes are per-fact verdicts applied through a separate surface,
    # so this must not enter the proposal/confirmation flow.
    "mutating": False,
    "function": {
        "name": "consolidate_memory",
        "description": (
            "Start (or continue) a project-memory consolidation run over a target "
            "document. Returns the FIRST portion: `reference` (the ONE attached "
            "reference text to read now — the material — and you cannot fetch "
            "anything else), `memory_index` (every fact already in the project's "
            "memory, as an index: id + title — check these BEFORE creating a new "
            "fact; fetch the bodies of the ones you are about to adjudicate via "
            "get_memory_facts), `merge_candidates` (the memory facts semantically "
            "NEAREST to this reference's material, scored, each WITH its body — the "
            "ones most likely to restate the fact you are about to extract, so merge "
            "into them instead of forking a near-duplicate twin), `progress` "
            "(`done`/`total`/`remaining` over the host's references — say where in "
            "the run you are), and `run_id` (cite it as provenance on every fact you "
            "write). Callable under any persona. The run is incremental: every portion "
            "after the first comes from next_reference, which stamps the completed "
            "reference and serves the next — and a run that was interrupted simply "
            "resumes from the first unstamped reference on the next call. If "
            "`reference` is null and `complete` is false, the run is WAITING on the "
            "next reference's transcription (`waiting_status`) — stop the loop and "
            "tell the user; resume later."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_doc_id": {
                    "type": "string",
                    "description": (
                        "The document whose attached references to consolidate. A "
                        "REFERENCE id is also accepted and re-resolves to its host "
                        "document — the run then covers that host's whole stack "
                        "(the named reference and all its siblings), because a "
                        "reference has no children of its own. The resolved target "
                        "comes back in the result."
                    ),
                },
            },
            "required": ["target_doc_id"],
        },
    },
}

# Agent-only for the same reason as CONSOLIDATE_MEMORY_TOOL (it belongs to the same
# credential-bearing loop). Non-mutating by design: the stamp it writes is a
# server-side CURSOR on the reference (mem_consolidated_at), not content the user
# could confirm — and first-writer-wins makes it idempotent, so it needs no
# sequential-execution guarantee either.
NEXT_REFERENCE_TOOL: dict = {
    "type": "function",
    "mutating": False,
    "function": {
        "name": "next_reference",
        "description": (
            "Continue a consolidation run. Stamps the window you just finished "
            "(`completed_reference_id` + `completed_window_index`) as consumed and "
            "returns the NEXT portion in the same shape as consolidate_memory (one "
            "`reference` + `memory_index` + `progress`), or `complete: true` with the "
            "run's totals when nothing is left. Call this after applying the verdicts "
            "for the window you just read — stamping happens here, so it never precedes "
            "the apply that justifies it. The loop: read the one window, extract, "
            "adjudicate, apply, call next_reference, repeat until `complete`. A "
            "reference longer than one window is served across several portions, so the "
            "next portion often carries the SAME reference at the next `window.index` — "
            "that is continuation, not a new reference. If the result has `reference` "
            "null and `complete` false, the run is WAITING on the next reference's "
            "transcription (`waiting_status`) — stop the loop and tell the user; "
            "resume later. The active run is resolved from the session automatically "
            "— do NOT pass run_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "completed_reference_id": {
                    "type": "string",
                    "description": (
                        "The reference whose window you just finished consolidating "
                        "— its verdicts are applied. Everything else the next portion "
                        "needs (the host, the project) is derived from this id."
                    ),
                },
                "completed_window_index": {
                    "type": "integer",
                    "description": (
                        "The `index` of the window you just finished, read from the "
                        "portion's `reference.window.index`. Pass it on every call so a "
                        "retried call after a lost response cannot skip a window — the "
                        "stamp advances only when the cursor still matches this index."
                    ),
                },
            },
            "required": ["completed_reference_id"],
        },
    },
}

# Agent-only for the same reason as CONSOLIDATE_MEMORY_TOOL (it belongs to the same
# credential-bearing loop), and additionally because it is a DIRECT write path that
# deliberately bypasses the proposal tier — the MCP surface is force-auto, so it has
# no gate of its own to substitute.
APPLY_MEMORY_VERDICTS_TOOL: dict = {
    "type": "function",
    # Mutating: it writes fact documents. This puts it in MUTATING_TOOLS, which is
    # what makes the dsh driver run it sequentially — the property that matters here,
    # since two concurrent applies over one fact are a read-compute-write race the
    # in-process lock only covers within a replica.
    "mutating": True,
    "function": {
        "name": "apply_memory_verdicts",
            "description": (
                "Apply per-fact verdicts from a consolidation run. Each verdict is one "
                "of: `new` (a fact not yet in memory — give title + text to create the "
                "fact document), `merge` (the same knowledge as an existing fact — give "
                "fact_id; add absorb_id to fold a second stored fact into it), "
                "`supersede` (this CORRECTS an existing fact in place — give fact_id + "
                "reason + text; the old wording is kept as history, never deleted; ask "
                "the user before superseding anything), or `skip` (give reason). "
            "Name a supersede/merge target ONLY in fact_id — an id written in fact "
            "text is never read as a target. Pass reference_id = the portion you are "
            "consolidating; the server stamps it as each fact's provenance. Applied as "
            "one batch; the result reports what was written and what was skipped. The "
            "active run is resolved from the session automatically — do NOT pass "
            "run_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reference_id": {
                    "type": "string",
                    "description": (
                        "The `reference.id` of the portion you are consolidating "
                        "right now. The server records it as the provenance of every "
                        "fact in this batch — provenance is never written by hand."
                    ),
                },
                "verdicts": {
                    "type": "array",
                    "description": (
                        "One entry per fact. Apply in batches that FIT the model's "
                        "output limit: split a batch only if it would be too large to "
                        "emit in one call (a batch that does not fit truncates mid-JSON "
                        "and never succeeds, however many times it is retried). The unit "
                        "of work is the reference — extract everything it carries, in "
                        "as many batches as fit. Invalid verdicts are NOT fatal to the "
                        "batch: valid ones apply, invalid ones come back in `rejected` "
                        "with the correction to make — fix ONLY those and re-emit just "
                        "them."
                    ),
                    "maxItems": MEMORY_VERDICTS_MAX_PER_BATCH,
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": [
                                    "new", "merge", "supersede", "skip",
                                ],
                            },
                            "fact_id": {
                                "type": ["string", "null"],
                                "description": (
                                    "The fact-doc id a `merge` or `supersede` "
                                    "targets (from `memory_index` or the apply "
                                    "result). A `new` verdict names no target."
                                ),
                            },
                            "absorb_id": {
                                "type": ["string", "null"],
                                "description": (
                                    "`merge` only: a SECOND fact-doc to fold into "
                                    "the target — must differ from fact_id (a fact "
                                    "cannot fold into itself; to reword one fact "
                                    "use `supersede`). The server picks the "
                                    "survivor (the more-established id); the "
                                    "absorbed fact retires pointing at it. Omit to "
                                    "absorb THIS portion's material into a single "
                                    "stored fact."
                                ),
                            },
                            "title": {
                                "type": ["string", "null"],
                                "description": (
                                    "The fact's subject-led title, "
                                    "`Субъект: краткая суть` (e.g. `Аспирин: "
                                    "антиагрегант при риске нарушения функции "
                                    "плаценты`). REQUIRED on `new`; on `supersede` "
                                    "it is optional and inherits the current title "
                                    "when omitted."
                                ),
                            },
                            "text": {
                                "type": ["string", "null"],
                                "description": (
                                    "The fact body, as PLAIN PROSE — the fact "
                                    "itself (one assertion). The body IS the fact: "
                                    "it is stored verbatim, never re-rendered."
                                ),
                            },
                            "reason": {"type": ["string", "null"]},
                        },
                        "required": ["action"],
                        # WHY: the per-action field
                        # requirements live IN THE SCHEMA, not only in description
                        # prose — the schema is the one surface the model can
                        # inspect, and a schema-valid object earning a 400 makes the
                        # model guess, and guessing retries the same bytes (session
                        # mem-acc-20260802b: four byte-identical re-emissions). The
                        # branches state the SAME rules as `memory/apply.py`'s
                        # validator (its INVARIANT); test_memory_verdict_schema
                        # derives the action list from this oneOf and binds both
                        # surfaces so they cannot drift apart.
                        "oneOf": [
                            {
                                "properties": {"action": {"const": "new"}},
                                "required": ["action", "text", "title"],
                            },
                            {
                                "properties": {"action": {"const": "merge"}},
                                "required": ["action", "fact_id"],
                            },
                            {
                                "properties": {"action": {"const": "supersede"}},
                                "required": [
                                    "action", "fact_id", "reason", "text",
                                ],
                            },
                            {
                                "properties": {"action": {"const": "skip"}},
                                "required": ["action", "reason"],
                            },
                        ],
                    },
                },
            },
            "required": ["reference_id", "verdicts"],
        },
    },
}


# Agent-only for the same reason as CONSOLIDATE_MEMORY_TOOL (same credential-bearing loop).
# Mutating: it moves the run cursor — clearing the consumed-stamps so a target's material
# can be walked again. This puts it in MUTATING_TOOLS (sequential execution) AND behind
# the apply gate (confirm ⇒ the call is held for the user's approval; repeats are bounded
# per target). Reach for it ONLY when the user asks to redo a target with a new accent or a
# different model — never as a step of the normal loop (a re-walk sees the existing facts
# as merge/supersede candidates, so it merges instead of forking twins).
REOPEN_CONSOLIDATION_TOOL: dict = {
    "type": "function",
    "mutating": True,
    "function": {
        "name": "reopen_consolidation",
        "description": (
            "Clear a target's consolidation stamps so its material can be walked again. "
            "Use ONLY when the user asks to re-consolidate a target they have already "
            "finished — to steer a second pass with a new focus (carried in the chat, "
            "not as a parameter) or under a different model. Never as a step of the "
            "normal loop. A reference id is accepted and re-resolves to its host (the "
            "clear covers the host's whole stack, like consolidate_memory). It writes "
            "NONE to both stamp fields and stops — no fact is touched, and nothing is "
            "recorded about why. The next consolidate_memory then re-serves every "
            "reference under that host, oldest-first; the existing facts appear as "
            "merge_candidates WITH their bodies, so overlapping material merges or "
            "supersedes instead of forking a duplicate twin."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "target_doc_id": {
                    "type": "string",
                    "description": (
                        "The document whose reference stack to re-open. A REFERENCE id "
                        "is also accepted and re-resolves to its host document — the "
                        "clear then covers that host's whole stack. Same resolution as "
                        "consolidate_memory."
                    ),
                },
            },
            "required": ["target_doc_id"],
        },
    },
}

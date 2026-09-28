"""Pydantic models: memory domain (split out of the former models.py)."""
from __future__ import annotations

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)

from config import (
    MEMORY_VERDICTS_MAX_PER_BATCH,
)
from models.tools import ToolApply


class ToolConsolidateMemory(BaseModel):
    """`consolidate_memory` body — start (or continue) a run over a target document's
    references; the first portion is one reference."""

    target_doc_id: str


class ToolReopenConsolidation(BaseModel):
    """`reopen_consolidation` body — clear a target's consolidation stamps so its material
    can be walked again under a new accent or model.

    # ARCH: the unit is the TARGET, same as `consolidate_memory`. A reference id is
    # accepted and re-resolves to its host (the clear covers the host's whole stack) —
    # reuse `resolve_consolidation_host`. The `apply` field gates the confirm/auto path
    # (mutating — it moves the run cursor, so confirmation is the default). Agent-only:
    # never served over external MCP (never added to AGENT_TOOLS), so this model is never
    # projected into the MCP schema.
    """

    target_doc_id: str
    apply: ToolApply = ToolApply.confirm


class ToolNextReference(BaseModel):
    """`next_reference` body — stamp the completed window, serve the next portion.

    # ARCH: the completed reference IS
    # the anchor — its `parent_id` is the host, so no run state or cursor table is
    # needed. Two fields, no mutually-exclusive overload.

    `run_id` is OPTIONAL on the agent surface: the loop tools resolve the session's active
    # run when it is omitted (a hand-copied 36-char run_id cost a whole batch on one
    # transcription slip). A sessionless caller must still pass it; the route 400s loudly.

    `completed_window_index` is the window the agent just finished (read from the served
    portion's `reference.window.index`). It makes a retried call after a lost response
    idempotent — the stamp advances only when the cursor still matches, so a lost
    response can never skip a window. Optional: a caller that does not track windows
    (MCP / legacy) gets unconditional advance (the accepted window-level crash-window).
    """

    run_id: str | None = None
    completed_reference_id: str
    completed_window_index: int | None = None


class ToolGetMemoryFacts(BaseModel):
    """`get_memory_facts` body — the fact bodies for the ids being adjudicated.

    # ARCH: the run payload's `memory_index` names every fact and carries no body;
    # this is the on-demand fetch. A read tool — the adjudication step uses it
    # exactly as it uses `read_document` for a document.
    """

    ids: list[str] = Field(min_length=1, max_length=100)


class ToolGetFactHistory(BaseModel):
    """`get_fact_history` body — the version history of one fact, on explicit request.

    The served payload carries only a revision marker (a count); this returns the
    retired wording + each predecessor's supersede reason + provenance. The agent
    calls it for a fact whose marker said it was revised. `fact_id` is the live fact
    the agent currently sees (a retired id is a 404 — it is a separate document).
    """

    fact_id: str


class MemoryVerdict(BaseModel):
    """One per-fact verdict.

    # WHY: `fact_id` is the ONLY way a supersede or merge names its target.
    # There is deliberately no field, and must never be a code path, that derives a
    # target from fact TEXT.  Why: naming a target by text would be ambiguous (two facts can share wording) and fragile to edits; the stable fact_id is the only identity, so merge/supersede never derive a target from text.
    # Why: a pattern matches the shape of a sentence, not its meaning, and what it
    # would decide here is which knowledge gets retired.

    # INVARIANT: `extra="forbid"` — an undeclared field is an ERROR, never dropped.
    # Why: the fact body and its marker are shipped into the agent's context, so an
    # unread field is a permanent per-fetch token cost. Forbidding it is what keeps
    # the verdict's field set equal to the set something reads.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "new", "merge", "supersede", "skip",
    ]
    # The fact-doc a merge or supersede targets (its `documents` id). A `new`
    # verdict names no target — it creates one from `title` + `text`.
    fact_id: str | None = None
    # `merge` only: a second fact-doc folded into the target. The server picks the
    # survivor (`select_survivor`); the absorbed fact retires pointing at it.
    absorb_id: str | None = None
    # The fact's subject-led title (`Субъект: краткая суть`, D3). Required on `new`
    # and on a supersede's successor.
    title: str | None = None
    # The fact body, as PLAIN PROSE. The body IS the fact — authored, never rendered.
    text: str | None = None
    reason: str | None = None


class ToolApplyMemoryVerdicts(BaseModel):
    """`apply_memory_verdicts` body — a batch applied under the project memory lock.

    `run_id` is OPTIONAL on the agent surface: the loop tools resolve the session's active
    # run when it is omitted (a hand-copied 36-char run_id cost a whole batch on one
    # transcription slip). A sessionless caller must still pass it; the route 400s loudly.
    """

    run_id: str | None = None
    # The reference this batch consolidated — the `reference.id` of the portion just
    # served. The server validates it and stamps it as every fact's provenance; a
    # verdict cannot write `sources` itself (see the INVARIANT in memory/apply.py).
    reference_id: str
    # WHY the tight ceiling: the emitting model runs at
    # maxTokens=8192, and a batch it cannot emit inside the output cap can never
    # succeed, however many times it is retried. The schema's stated maximum and
    # this bound derive from the SAME constant — never hand-copied.
    verdicts: list[MemoryVerdict] = Field(
        min_length=1, max_length=MEMORY_VERDICTS_MAX_PER_BATCH,
    )

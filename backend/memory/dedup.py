"""Refusing a `new` fact that restates one already stored.

# ARCH: the duplicate decision is the SERVER's, not the agent's. Measured on a
# re-served portion, the agent chose `new` over `merge` 8 times out of 9 — three of
# them against twins whose text the same portion already served to it. Supplying
# the material is necessary and not sufficient; the same pairs separate trivially by
# cosine (every one at >= 0.87). This is also what makes the resume design's promise
# true: a run that dies mid-way leaves its references unstamped, so they are re-served,
# and the new facts meet their twins as merge candidates — a merge, not a duplicate
# (memory/task_builder.py).

# ARCH: memory — dedup gate, fact level. The gate refuses a `new` verdict whose body
# restates a stored FACT. Each new fact is scored against its OWN nearest neighbours
# (never a pooled comparison set against the STORED index) using the SAME vector index
# that serves the portion's merge candidates (embeddings.nearest_memory_facts). One
# source of truth for what is close: the stored `doc_chunks` vectors. The gate embeds
# only the incoming fact; it never re-embeds a stored candidate body, so a `new` refused
# here and a candidate served there can never disagree on proximity.
#
# ARCH: memory — dedup gate, intra-batch pass. The stored-index pass is blind to twins
# arriving in the SAME batch: neither is stored yet, so each scores the other at zero.
# At five verdicts a batch that is a small risk; once the extraction quota is removed
# and a batch grows to a reference's worth, it is the first thing that breaks — two
# twins land and read as distinct facts. The intra-batch pass closes it: a pairwise
# cosine over the SAME vectors the stored pass already embedded (no second round-trip,
# no new dependency), run AFTER the stored pass so a stored refusal is never taken as a
# canonical twin for a sibling. Fail-open is unaffected — the pass is pure arithmetic
# over vectors that only exist once embedding succeeded.

# ARCH: near-duplicate facts that drift together AFTER apply — two stored facts whose
# accumulated wording converges until they restate one another — are NOT re-checked
# here. Drift detection is additive infrastructure (a WORK queue over converged pairs)
# with no consumer yet; the merge candidates served with each portion are the agent's
# existing judgment channel for same-knowledge facts. Expect drift to resurface as the
# next problem; do not pre-build for it.

# WHY: the gate FAILS OPEN. Any embedding failure writes the fact.
# Why: a dedup check that fails closed converts a degraded dependency into total data
# loss for the run — the portion's material is consumed and its facts are refused,
# with nothing to retry against. A duplicate is repairable by a later merge; a fact
# never written is gone.

# INVARIANT: a fact's embedding lives on its `doc_chunks` row, never on the fact.
# Why: the fact body ships verbatim into context on each fetch, and the gate reads the
# embedding from the index query — an embedding on the fact would be the largest
# unread field yet.
"""
from __future__ import annotations

import logging
import math

import settings

logger = logging.getLogger(__name__)


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity, zero on a zero vector (the safe value for 'unrelated', so a
    degenerate embedding can never read as a duplicate of everything)."""
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _gated_verdicts(verdicts: list[dict]) -> list[tuple[int, dict]]:
    """The verdicts this gate judges: `new` ones carrying text.

    `supersede` is deliberately NOT gated — it REPLACES the very fact it resembles,
    so resembling it is the point and gating would make the action unusable. `merge`
    carries no text of its own.
    """
    return [
        (i, v) for i, v in enumerate(verdicts)
        if v.get("action") == "new" and (v.get("text") or "").strip()
    ]


async def _fact_body(project_id: str, doc_id: str) -> str:
    """The body of a stored fact — the 'Stored: …' quote in the rejection.

    Fetched only for a fact the gate is about to refuse (one twin), never for the
    whole comparison set: the body is context for the agent's next attempt, not part
    of the similarity decision (that is the stored vector's job)."""
    from db import get_db

    db = await get_db()
    rows = await db.query(
        "SELECT content FROM documents WHERE project_id = $p "
        "AND meta::id(id) = $id AND deleted_at IS NONE",
        {"p": project_id, "id": doc_id},
    )
    if not rows:
        return ""
    return (rows[0].get("content") or "").strip()


def _rejection(twin: dict, score: float) -> str:
    """Name the twin and the action that resolves it.

    An error that does not discriminate the next attempt from the last one turns one
    refused verdict into an unbounded loop, so the message carries the id a `merge`
    needs and the stored wording the agent must compare against.
    """
    return (
        f"This restates a fact already stored as {twin['title']!r} "
        f"(similarity {score:.2f}) — memory already knows it. If it is the SAME "
        f'knowledge, send action "merge" with fact_id {twin["id"]} instead, '
        f"which is what records the second source. If your fact genuinely says "
        f"something the stored one does not, say that different thing in its own "
        f"words. Stored: {twin['text'][:200]!r}"
    )


async def _find_twin(
    project_id: str, query_vec: list[float], threshold: float,
) -> dict | None:
    """The stored fact this new text restates, or None.

    The top neighbour — highest cosine — is the only one that can clear the threshold
    (ranking is DESC), so one comparison settles it, and there is no pooled set for one
    new fact to be matched against another's neighbours. Best-effort by construction:
    an empty neighbour list means no twin, and `nearest_memory_facts` already returns
    [] on any query miss, so the fail-open contract holds."""
    import embeddings

    neighbours = await embeddings.nearest_memory_facts(
        project_id=project_id, query_vec=query_vec,
    )
    if not neighbours:
        return None
    top = neighbours[0]
    if top["score"] < threshold:
        return None
    return {**top, "text": await _fact_body(project_id, top["id"])}


def _intra_batch_rejection(twin: dict, score: float) -> str:
    """Name the sibling verdict in THIS batch and the action that resolves it.

    Unlike a stored twin, the sibling is not yet a fact, so there is no `fact_id` to
    merge into — the resolution is to DROP one of the two, not merge. Saying `merge`
    here would point the agent at an id that does not exist yet, which is the dead loop
    this gate exists to prevent."""
    return (
        f"This restates another verdict in THIS SAME batch "
        f"({(twin.get('title') or twin.get('text') or '')[:80]!r}, similarity "
        f"{score:.2f}) — both say the same thing. Keep one and drop the other; if it "
        f"genuinely adds something the other does not, say that different thing in its "
        f"own words."
    )


def _intra_batch_errors(
    gated: list[tuple[int, dict]], vecs: list[list[float]], *,
    threshold: float, refused: dict[int, str],
) -> dict[int, str]:
    """`{verdict index: rejection}` for each `new` fact that restates an EARLIER `new`
    fact in the SAME batch.

    Pure over the vectors the stored pass already embedded — no async, no DB — so it is
    testable in isolation and adds nothing to the fail-open surface. The surviving twin
    is the EARLIEST verdict the stored pass let through (`refused` excludes stored twins,
    and a sibling this pass has already refused is excluded too), so each cluster keeps
    exactly one canonical representative rather than refusing every member against a
    twin that is itself refused."""
    errors: dict[int, str] = {}
    for a in range(len(gated)):
        idx_a = gated[a][0]
        if idx_a in refused or idx_a in errors:
            continue
        for b in range(a):
            idx_b = gated[b][0]
            if idx_b in refused or idx_b in errors:
                continue
            score = _cosine(vecs[a], vecs[b])
            if score >= threshold:
                errors[idx_a] = _intra_batch_rejection(gated[b][1], score)
                logger.info(
                    "memory dedup gate: verdict %d refused — intra-batch twin of "
                    "verdict %d at %.3f", idx_a, idx_b, score,
                )
                break
    return errors


async def duplicate_fact_errors(
    verdicts: list[dict], *, project_id: str,
) -> dict[int, str]:
    """`{verdict index: rejection}` for every `new` fact that restates a stored one.

    Best-effort by construction (see the fail-open INVARIANT): any failure returns an
    empty mapping, so the batch applies exactly as it did before this gate existed.
    """
    import embeddings

    threshold = await settings.get("MEMORY_DUPLICATE_FACT_THRESHOLD")
    gated = _gated_verdicts(verdicts)
    if not gated:
        return {}
    try:
        await embeddings._ensure_config()
        new_vecs = await embeddings.embed_texts(
            [v.get("text") or "" for _i, v in gated]
        )
    except Exception:
        logger.warning(
            "memory dedup gate: unavailable, facts apply ungated", exc_info=True,
        )
        return {}

    errors: dict[int, str] = {}
    for (index, _v), vec in zip(gated, new_vecs):
        twin = await _find_twin(project_id, vec, threshold)
        if twin is not None:
            errors[index] = _rejection(twin, twin["score"])
            logger.info(
                "memory dedup gate: verdict %d refused — %.3f against fact %s",
                index, twin["score"], twin["id"],
            )
    # Intra-batch: pairwise over the same vectors, survivors only. A stored-refused
    # verdict (in `errors`) is neither canonical nor a candidate — run AFTER the stored
    # pass so the two never disagree on which twin of a pair survives.
    errors.update(_intra_batch_errors(
        gated, new_vecs,
        threshold=threshold, refused=errors,
    ))
    return errors


__all__ = ["duplicate_fact_errors"]

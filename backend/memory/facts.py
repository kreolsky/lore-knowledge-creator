"""Pure fact-level transitions: new / merge / supersede, survivor choice, history.

# ARCH: every function here is PURE (dict in, dict out) — no DB, no clock reads
# beyond an injected `now`. The apply orchestration (locking, scope, persistence)
# lives in `apply.py`. Why the split: these are the rules the design argues for at
# length — survivor selection, the verification stamp, the retirement link — and
# rules that can only be exercised through an HTTP round-trip do not get tested at
# the edges where they are actually wrong.

# ARCH: memory — fact transitions. A FACT is a document (`is_memory = true`) whose
# `content` IS the fact (Order 2: the entity level was deleted — there is one
# identity level, not two). The `mem` object these functions build and read carries
# the fact's metadata (provenance, survivor fields, version history); the body is
# authored prose, never a projection, so there is no render step and no latch.

# WHY: a fact's `mem` object carries ONLY fields something reads.
# Why: `get_memory_facts` serves the fact's body verbatim and the marker fields
# alongside it, so an unread `mem` member is per-fetch token cost on the one payload
# the incremental run exists to keep small. The members below each have a reader:
# `merge_count` / `created_at` / `last_verified_at` (`select_survivor`),
# `provenance` (the recoverability thread), `version_history` (the on-request
# history channel), and the retirement triple (`superseded_by` / `supersede_reason`
# / `superseded_at` — the undo payload for a wrong machine-chosen merge; no
# production channel surfaces it yet).

# INVARIANT: read every optional `mem` field with `.get()`, never `mem[...]`.
# Why: SurrealDB does NOT persist object keys whose value is NONE, so the null fields
# written here (`superseded_by`, `supersede_reason`, `superseded_at`) come back
# ABSENT rather than None. The shape below is the authored shape, not the stored one;
# subscripting a never-set field KeyErrors on exactly the facts that behave normally.
"""
from __future__ import annotations


def new_fact_mem(
    *, text: str, sources: list[dict], run_id: str, now: str,
    history: list[dict] | None = None,
) -> dict:
    """Build the `mem` object for a freshly-created fact.

    `history` is carried only by a supersede's SUCCESSOR (the victim's prior
    revisions move onto it); a brand-new fact passes nothing and starts with no
    history.

    # INVARIANT: `last_verified_at` is set on creation and on content-carrying saves
    # ONLY — never on a retirement (see `merge_into_fact`).
    # Why: a re-verification sweep orders by this field, so it must mean "when this
    # knowledge was last asserted", not "when the row was last touched".
    """
    return {
        "merge_count": 0,
        "created_at": now,
        "last_verified_at": now,
        "provenance": {
            "run_id": run_id,
            "sources": list(sources or []),
        },
        "version_history": list(history or []),
        "superseded_by": None,
        "supersede_reason": None,
        "superseded_at": None,
    }


def _distinct_source_ids(sources: list[dict] | None) -> set[str]:
    return {
        str(s.get("id")) for s in (sources or [])
        if isinstance(s, dict) and s.get("id")
    }


def _merged_sources(survivor: dict, sources: list[dict]) -> list[dict]:
    """The survivor's source list after absorbing `sources` (deduped by id, order
    preserved). Provenance is the one thread from a fact back to the material it came
    from; it only ever grows through this path."""
    prov = survivor.get("provenance") or {}
    known = _distinct_source_ids(prov.get("sources"))
    out = list(prov.get("sources") or [])
    for s in (sources or []):
        if isinstance(s, dict) and str(s.get("id")) not in known:
            out.append(s)
            known.add(str(s.get("id")))
    return out


def merge_into_fact(
    survivor: dict, *, survivor_body: str, text: str | None,
    sources: list[dict], run_id: str, now: str,
) -> dict:
    """Absorb incoming material into `survivor`'s `mem`; return the updated mem.

    `survivor_body` is the fact's current `content` — passed in (not read from `mem`)
    so the content-change test compares against the real body. Whether the caller
    also rewrites the body is the caller's concern (it routes content through the
    collab surface); this function only updates `mem`.

    A merge is emitted when the agent MEETS material it has already consumed: given a
    portion it has never seen, it files `new`; re-serving one consumed reference is
    what produces a `merge`. So `merge_count` reads 0 across a memory built by first
    passes alone; that is the walk being unrepeated, not the verdict being dead, and
    `select_survivor`'s first stage depends on the difference.

    # WHY: `last_verified_at` moves only when the save CARRIES CONTENT — either
    # new wording or a genuinely new source corroborating the same content.
    # Why: otherwise a fact untouched for a year looks freshly confirmed because
    # something administrative happened to it.
    """
    updated = dict(survivor)
    prov = dict(updated.get("provenance") or {})
    # WHY: `provenance.run_id` names the run that touched the fact LAST, not the
    # one that created it — a merge deliberately overwrites it.
    # Why: the field answers "which run is answerable for this wording", and after a
    # merge that is the merging run. The consequence is the trap: a run's own output
    # cannot be recovered by grouping live facts on `run_id` (a later merge steals
    # them), so measuring a run's delta reads `mem.created_at` and a snapshot diff.
    prov["run_id"] = run_id
    updated["merge_count"] = int(updated.get("merge_count") or 0) + 1

    carries_content = bool(text) and text != survivor_body
    incoming = _merged_sources(survivor, sources)
    new_ids = _distinct_source_ids(sources) - _distinct_source_ids(
        (survivor.get("provenance") or {}).get("sources")
    )
    if incoming != list((survivor.get("provenance") or {}).get("sources") or []):
        prov["sources"] = incoming
    updated["provenance"] = prov

    if carries_content or new_ids:
        updated["last_verified_at"] = now
    return updated


def supersede_fact_mem(
    victim: dict, victim_body: str, *, text: str, sources: list[dict],
    run_id: str, reason: str | None, now: str,
) -> dict:
    """Rewrite a fact IN PLACE for a correction — the doc id stays, so it remains its
    own stable link target; nothing is retired and no successor doc is created.

    The victim's CURRENT wording (`victim_body`) moves onto `version_history` (one
    entry, newest-first), the new `sources` are folded into provenance, `run_id` is
    re-stamped, and `last_verified_at` advances. The body itself (`content`) is
    routed by the caller, not touched here — this function only updates `mem`.

    # WHY: the supersede target is the FIELD the caller resolved (`fact_id`).
    # No code path may promote an id found in fact TEXT to a structural target.
    # Why: a pattern matches the shape of a sentence, not its meaning, and what it
    # would decide here is which knowledge gets retired. Enforced by construction —
    # this function never reads fact text.

    # WHY: an in-place supersede ADVANCES `last_verified_at`.
    # Why: a correction IS a re-assertion of this knowledge (the wording that replaces
    # the old one is what the agent now vouches for), unlike a retirement (merge-
    # absorb) which is administrative and must not move the stamp.
    """
    updated = dict(victim)
    updated["version_history"] = successor_history(
        victim, victim_body, reason=reason, now=now,
    )
    prov = dict(updated.get("provenance") or {})
    prov["run_id"] = run_id
    prov["sources"] = _merged_sources(victim, sources)
    updated["provenance"] = prov
    updated["last_verified_at"] = now
    return updated


def successor_history(victim: dict, victim_body: str, *, reason: str | None, now: str) -> list[dict]:
    """The supersede's new `version_history`: the victim's current wording prepended to
    its own prior history, newest-first.

    The entry records the wording being retired, the moment, the reason the agent
    gave for replacing it, and the provenance that wording carried. The victim row
    keeps its `content` too (it is the same doc, rewritten in place) — this list is
    what the on-request history channel (`get_fact_history`) serves.
    """
    entry = {
        "text": victim_body,
        "superseded_at": now,
        "supersede_reason": reason,
        "run_id": (victim.get("provenance") or {}).get("run_id"),
        "sources": list((victim.get("provenance") or {}).get("sources") or []),
    }
    return [entry, *list(victim.get("version_history") or [])]


def retire_fact_mem(mem: dict, successor_id: str, *, reason: str | None, now: str) -> dict:
    """Retire a fact absorbed by a merge — stamp the retirement triple and point it
    at the survivor. This is the ONLY retirement path; a supersede rewrites in
    place instead.

    The retired fact KEEPS its content and its version_history — a link to it
    still resolves (link stability; `_retire_fact` soft-deletes the row and
    drops its vector, the retrieval slot — abot2's one-operation `retire`).
    `superseded_by` is written but surfaced by no channel: it is the undo payload
    for a wrong machine-chosen merge, not a reporting feature.

    # WHY: retiring a fact does NOT move `last_verified_at`.
    # Why: a retirement is administrative (the knowledge moved elsewhere), not a
    # re-verification (see `merge_into_fact`).
    """
    retired = dict(mem)
    retired["superseded_by"] = successor_id
    retired["supersede_reason"] = reason
    retired["superseded_at"] = now
    return retired


def select_survivor(
    a_id: str, a: dict, b_id: str, b: dict,
) -> tuple[str, str]:
    """Return `(survivor_id, absorbed_id)` for two facts being merged.

    # INVARIANT(corruption): the survivor is chosen by THIS rule, never by which id the caller
    # named first: established hub → most recently verified → oldest.
    # Why: the hub is the fact others were already merged into, so references have
    # accumulated on it — retiring it would orphan the most-attested row. Making the
    # choice structural rather than positional stops the outcome depending on how the
    # agent happened to phrase its verdict.
    """
    # 1. Established hub — the one others were already merged into.
    ma, mb = int(a.get("merge_count") or 0), int(b.get("merge_count") or 0)
    if ma != mb:
        return (a_id, b_id) if ma > mb else (b_id, a_id)
    # 2. Most recently verified. ISO-8601 strings compare lexicographically.
    va, vb = str(a.get("last_verified_at") or ""), str(b.get("last_verified_at") or "")
    if va != vb:
        return (a_id, b_id) if va > vb else (b_id, a_id)
    # 3. Oldest wins — it has been around longest, so prose has had longest to
    # accumulate around it, and it is the likeliest to be referenced somewhere.
    ca, cb = str(a.get("created_at") or ""), str(b.get("created_at") or "")
    return (a_id, b_id) if ca <= cb else (b_id, a_id)


# ─── Reading the revision history ─────────────────────────────────────────────
#
# A fact's `version_history` is the retired wording the on-request channel serves.
# These readers are PURE (mem in, marker/chain out): the always-served MARKER
# (revisions count) and the explicit-request history list, both testable without DB.


def fact_revisions(mem: dict) -> tuple[int, str | None]:
    """The served fact MARKER: `(revisions, last_revised_at)`.

    `revisions` is the count of prior wordings on the version-history chain;
    `last_revised_at` is the most recent predecessor's `superseded_at` — the moment
    the current wording replaced the previous one. Returns `(0, None)` for a fact
    nobody ever superseded, so the common fact pays nothing: the marker is OMITTED at
    N=0 (`_serve_fact`), never served as `revisions: 0`.
    """
    history = list((mem or {}).get("version_history") or [])
    if not history:
        return 0, None
    return len(history), history[0].get("superseded_at")


def fact_history(mem: dict) -> list[dict]:
    """The fact's version history as served dicts, newest-first.

    Each entry carries the retired `text`, the supersede link's own fields
    (`superseded_at`, `supersede_reason`) and provenance (`run_id`, `sources`) — the
    `get_fact_history` tool's payload. This is the ONLY channel through which a
    retired fact's wording reaches the agent; the served portion payload carries the
    marker (a count), never the retired text.
    """
    return [
        {
            "text": c.get("text") or "",
            "superseded_at": c.get("superseded_at"),
            "supersede_reason": c.get("supersede_reason"),
            "run_id": c.get("run_id"),
            "sources": list(c.get("sources") or []),
        }
        for c in (mem or {}).get("version_history") or []
    ]


__all__ = [
    "new_fact_mem", "merge_into_fact", "supersede_fact_mem", "retire_fact_mem",
    "successor_history", "select_survivor", "fact_revisions", "fact_history",
]

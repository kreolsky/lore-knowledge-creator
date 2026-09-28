"""Fact resolution + per-verdict application (the apply body).

Leaf of apply (imports _apply_validation): scope-checked fact loading, fact-doc
creation, content routing, retirement (the vector drop), and the per-verdict apply
loop. NEVER calls the compensation helpers (those + their callers stay in apply so
the monkeypatched apply-namespace seams resolve). apply re-exports _resolve_run.

# ARCH: staleness is a pre-lock REJECTION, not a whole-batch 404. A fact address
# that no longer resolves in the live memory space (missing, soft-deleted, retired,
# merged away, wrong kind, cross-project) is named per verdict by
# `_unresolvable_fact_errors` — read-only, decided before the lock alongside the
# duplicate gate, reported through the `rejected` list with the one-turn remedy.
# The scope WALL is the deliberate exception: it stays a whole-call hard failure
# here (`_require_fact_in_scope`), because a boundary is not staleness.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import NamedTuple

from agent import collab_writes, doc_state
from fastapi import HTTPException

from db import fetch_one, get_db
from memory.facts import (
    merge_into_fact,
    new_fact_mem,
    retire_fact_mem,
    select_survivor,
    supersede_fact_mem,
)

UNKNOWN_RUN_DETAIL = (
    "Unknown consolidation run — runs are resumable: call consolidate_memory on the "
    "target; completed references are not re-served"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mem_of(doc: dict) -> dict:
    return dict(doc.get("mem") or {})


class RunResolution(NamedTuple):
    """One api_keys read's yield: the scope wall + the run's owning user."""

    scope_root: str
    user_id: str


async def _resolve_run(project_id: str, run_id: str) -> RunResolution:
    """Resolve the run's `scope_root` + owning user from its key row (ONE read);
    404 on an unknown run.

    # INVARIANT(security): the wall is read from the RUN KEY ROW, never from a caller-supplied
    # scope and never from a held plaintext token.
    # Why: the run key's plaintext is discarded at mint time and is unrecoverable, so
    # code written to "use the run's token" fails at the moment of use and invites the
    # repair that persists or caches it — which is what would turn a scope record into
    # a real long-lived bearer credential. Reading `document_id` off the row needs no
    # secret at all.
    """
    db = await get_db()
    rows = await db.query(
        "SELECT project_id, document_id, deleted_at, user_id "
        "FROM type::record('api_keys', $id)",
        {"id": run_id},
    )
    if not rows or rows[0].get("deleted_at"):
        raise HTTPException(status_code=404, detail=UNKNOWN_RUN_DETAIL)
    row = rows[0]
    if row.get("project_id") != project_id:
        # Uniform 404 for missing AND cross-project — no existence oracle.
        raise HTTPException(status_code=404, detail=UNKNOWN_RUN_DETAIL)
    scope_root = row.get("document_id") or ""
    if not scope_root:
        # A whole-project key is NOT a consolidation run. Refused loudly rather than
        # treated as "unscoped": silently accepting one would let a chat session's
        # own key write memory anywhere in the project.
        raise HTTPException(
            status_code=403, detail="This run is not scoped to the Memory folder",
        )
    return RunResolution(scope_root=scope_root, user_id=row.get("user_id") or "")


async def _require_fact_in_scope(scope_root: str, doc_id: str, project_id: str) -> dict:
    """Fetch a fact document and re-assert the wall on it.

    A `fact_id` in a verdict is agent-supplied input like any other, so the wall is
    re-checked per target here even though the run key already carries it — this path
    does not go through the Tool-API routes that normally do it.

    # INVARIANT(security): the wall wins over a stale id — a fact_id naming a document
    # outside `scope_root` fails the batch here, and is never touched.  Why: a fact_id could name a document moved outside scope_root; the scope wall rejects it here rather than mutating a fact the caller can no longer see.
    """
    from scope import require_doc_in_scope

    # INVARIANT(staleness): `deleted_at` is re-asserted HERE, under the lock, even
    # though the pre-lock staleness gate already refused stale ids.
    # Why: the gate and this read are not atomic — a concurrent apply on another
    # replica (the lock is per-process, see apply's LIMITATION) can retire the fact
    # in between, and writing through a zombie target would resurrect a fact every
    # read channel refuses. Retirement soft-deletes, so a retired fact is refused by
    # this same clause that refuses a user deletion — one axis, one check (see
    # `_retire_fact`).
    doc = await fetch_one("documents", doc_id)
    if not doc or doc.get("deleted_at") or doc.get("project_id") != project_id:
        raise HTTPException(status_code=404, detail="Fact document not found")
    if not doc.get("is_memory"):
        raise HTTPException(status_code=404, detail="Fact document not found")
    await require_doc_in_scope(scope_root, doc_id)
    return doc


_STALE_FACT_REASON = (
    "no longer resolves (retired, merged, or unknown) — re-read the live facts via "
    "get_memory_facts, then re-address or drop the verdict"
)


def _fact_addresses(v: dict) -> list[tuple[str, str]]:
    """The (field, id) fact addresses a verdict will resolve at apply time — exactly
    the fields `_require_fact_in_scope` is asked to resolve under the lock."""
    addresses: list[tuple[str, str]] = []
    if v.get("action") in ("merge", "supersede"):
        fact_id = (v.get("fact_id") or "").strip()
        if fact_id:
            addresses.append(("fact_id", fact_id))
        absorb_id = (v.get("absorb_id") or "").strip()
        if v.get("action") == "merge" and absorb_id:
            addresses.append(("absorb_id", absorb_id))
    return addresses


async def _unresolvable_fact_errors(
    verdicts: list[dict], *, project_id: str,
) -> dict[int, str]:
    """`{verdict index: rejection}` for every fact address that no longer resolves
    in the project's LIVE memory space — the staleness gate, layer (c) of the unified
    address-failure contract: it names WHICH verdict and WHICH id failed, plus the
    one-turn remedy (re-read the live index via get_memory_facts).

    Read-only like the duplicate gate, so it runs BEFORE the lock (see
    `_resolve_apply_inputs`): a stale id becomes a per-verdict REJECTION the model
    corrects incrementally — the 200-with-rejections / 400-only-when-nothing-valid
    semantics are unchanged; only the source of the refusal moves (whole-call 404 →
    named rejection). One uniform reason for every cause — missing, soft-deleted,
    retired, cross-project, non-memory — the same information the old 404 carried
    (no existence oracle), now per-verdict. A RETIRED fact is included deliberately:
    the index and get_memory_facts already refuse it, so an id that resolves only in
    the documents table is a zombie target, not a fact the agent can still see.
    User-confirmed semantics (2026-08-15): write-access matches read-access — a
    retired fact is NEVER a verdict target; do not "fix" the predicate back to
    existence-only.

    # INVARIANT(security): the scope wall is NOT checked here — it stays a whole-call
    # hard failure at persistence (`_require_fact_in_scope`).  Why: folding the wall
    # into per-verdict rejections would let a batch carrying an out-of-scope target
    # proceed and report the rest as applied — the wall must stop the call, never be
    # corrected around.
    """
    ids = list({fid for v in verdicts for _field, fid in _fact_addresses(v)})
    if not ids:
        return {}
    db = await get_db()
    rows = await db.query(
        "SELECT meta::id(id) AS id FROM documents WHERE project_id = $pid "
        "AND meta::id(id) IN $ids AND is_memory = true "
        "AND deleted_at IS NONE",
        {"pid": project_id, "ids": ids},
    )
    live = {r["id"] for r in (rows or [])}
    errors: dict[int, str] = {}
    for i, v in enumerate(verdicts):
        # First failing address names the verdict (fact_id outranks absorb_id). The
        # `verdict {i}:` index prefix is added UNIFORMLY by _preflight_verdicts —
        # every gate's reason carries it, none embeds it itself.
        for field, fid in _fact_addresses(v):
            if fid not in live and i not in errors:
                errors[i] = f"{field} {fid} {_STALE_FACT_REASON}"
    return errors


async def _create_fact(
    *, project_id: str, scope_root: str, title: str, text: str, mem: dict, user: dict,
) -> str:
    """Create a fresh fact document inside the Memory folder. The body IS the fact
    (authored prose passed as `content`), so it is never re-rendered.

    # WHY(one-door): this is the ONLY path that creates an `is_memory` document.  Why: centralizing is_memory creation in one door keeps the memory-write invariants checkable in one place and lets the one-door test assert no other path mints a fact-doc.
    # Why (D9): a test reads the source and fails when anything else mints a fact-doc.

    # WHY: a fact carries no verification state beyond live/retired, and a
    # hand-edited fact is not distinguished from a machine-authored one.
    # Why: hand-editing memory is not an expected workflow and is deliberately NOT
    # blocked; the product does not want facts flagged as unverified, so there is
    # nothing to detect and no third state to carry. (The old `unverified` search
    # marker fired on retired facts only — already hidden by the retirement
    # soft-delete — while claiming to mean "hand-edited", which its computation
    # never measured.)
    """
    created = await collab_writes.create_document_via_collab(
        title=title, content=text, parent_id=None, project_id=project_id,
        user=user, scope_root=scope_root,
    )
    doc_id = created["doc_id"]
    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET is_memory = true, "
        "mem_active = true, mem = $mem",
        {"id": doc_id, "mem": mem},
    )
    return doc_id


async def _persist_mem(doc_id: str, mem: dict, *, title: str | None = None) -> None:
    """Write a fact's `mem` (and optionally its title). The body lives in `content`,
    routed separately by the caller when it changes."""
    db = await get_db()
    if title is not None:
        await db.query(
            "UPDATE type::record('documents', $id) SET mem = $mem, title = $title",
            {"id": doc_id, "mem": mem, "title": title},
        )
    else:
        await db.query(
            "UPDATE type::record('documents', $id) SET mem = $mem",
            {"id": doc_id, "mem": mem},
        )


async def _route_content(doc_id: str, content: str, project_id: str) -> None:
    """Route a fact's new body through the single content-convergence path (the same
    one an in-editor edit uses), so mentions + the embedding debounce fire."""
    await doc_state.route_document_content(
        doc_id=doc_id, new_content=content, project_id=project_id,
    )


async def _retire_fact(
    doc_id: str, mem: dict, successor_id: str, *, reason: str | None, now: str,
) -> None:
    """Retire a fact absorbed by a merge: soft-delete it, mark the label, point at
    the survivor, and DROP THE VECTOR. The document and its content stay — a link
    to it still resolves; only the retrieval slot is removed (abot2's one-op
    `retire`)."""
    retired = retire_fact_mem(mem, successor_id, reason=reason, now=now)
    db = await get_db()
    # INVARIANT(persisted): retiring a fact SOFT-DELETES it — `deleted_at` is what hides it.
    # Why: `mem_active = false` is only the label that tells a merge-retirement apart
    # from a user deletion (memory/stats.py, idx_documents_memory); readers filter
    # `deleted_at` project-wide and kept forgetting the memory-only `mem_active`
    # clause — two axes leaked retired facts into search_materials AND
    # _allowed_fact_ids. One axis is what makes every reader agree by construction.
    # Operator-confirmed: RESTORING a retired fact (clearing `deleted_at`) legitimately
    # returns it to every reader — restore means "make visible again", and the
    # `mem_active = false` label merely keeps it countable under `retired` in stats.
    # Do not add a restore-time `mem_active` flip or a memory-only reader carve-out.
    await db.query(
        "UPDATE type::record('documents', $id) SET mem_active = false, "
        "deleted_at = time::now(), mem = $mem",
        {"id": doc_id, "mem": retired},
    )
    await db.query("DELETE doc_chunks WHERE document_id = $id", {"id": doc_id})


def _sources_of(v: dict) -> list[dict]:
    """The reference provenance stamped onto every verdict before apply (see apply's
    `_stamp_provenance` INVARIANT) — subscripted, not `.get`, so a path that ever
    skips the stamp fails here instead of silently writing a fact with no provenance."""
    return list(v["sources"])


async def _apply_new(
    v: dict, *, project_id: str, scope_root: str, run_id: str, now: str, user: dict,
) -> str:
    """Create a fact-doc from the verdict's `title` + `text`. Returns the new doc id."""
    mem = new_fact_mem(
        text=v["text"], sources=_sources_of(v), run_id=run_id, now=now,
    )
    return await _create_fact(
        project_id=project_id, scope_root=scope_root, title=v["title"],
        text=v["text"], mem=mem, user=user,
    )


async def _apply_merge(
    v: dict, *, project_id: str, scope_root: str, run_id: str, now: str,
) -> list[str]:
    """Absorb material into a stored fact, or fold two stored facts. Returns the ids
    touched (the survivor, and — when two are folded — the absorbed fact too)."""
    target = await _require_fact_in_scope(scope_root, v["fact_id"], project_id)
    target_mem = _mem_of(target)
    target_body = target.get("content") or ""
    absorb_id = (v.get("absorb_id") or "").strip() or None

    if absorb_id is None:
        # Absorb THIS portion's material into the stored fact: update wording (if the
        # agent sent merged text) and record the second source.
        updated = merge_into_fact(
            target_mem, survivor_body=target_body, text=v.get("text"),
            sources=_sources_of(v), run_id=run_id, now=now,
        )
        await _persist_mem(v["fact_id"], updated)
        if v.get("text") and v["text"] != target_body:
            await _route_content(v["fact_id"], v["text"], project_id)
        return [v["fact_id"]]

    absorb = await _require_fact_in_scope(scope_root, absorb_id, project_id)
    # Defence in depth: the pure preflight already rejects absorb_id == fact_id as a
    # per-verdict rejection (see _validate_verdict); this mid-lock raise is the
    # second surface of the same rule, unreachable through the normal path.
    if absorb_id == v["fact_id"]:
        raise HTTPException(status_code=400, detail="absorb_id must differ from fact_id")
    absorb_mem = _mem_of(absorb)
    absorb_body = absorb.get("content") or ""
    survivor_id, absorbed_id = select_survivor(
        v["fact_id"], target_mem, absorb_id, absorb_mem,
    )
    survivor_mem = target_mem if survivor_id == v["fact_id"] else absorb_mem
    survivor_body = target_body if survivor_id == v["fact_id"] else absorb_body
    absorb_sources = (absorb_mem.get("provenance") or {}).get("sources") or []
    updated = merge_into_fact(
        survivor_mem, survivor_body=survivor_body, text=v.get("text"),
        sources=[*_sources_of(v), *absorb_sources], run_id=run_id, now=now,
    )
    await _persist_mem(survivor_id, updated)
    if v.get("text") and v["text"] != survivor_body:
        await _route_content(survivor_id, v["text"], project_id)
    # The absorbed fact RETIRES pointing at the survivor — never hard-deleted;
    # `superseded_by` is the undo payload, not a reporting channel.
    await _retire_fact(
        absorbed_id, absorb_mem if absorbed_id == absorb_id else target_mem,
        survivor_id, reason="merged into the surviving fact", now=now,
    )
    return [survivor_id, absorbed_id]


async def _apply_supersede(
    v: dict, *, project_id: str, scope_root: str, run_id: str, now: str,
) -> list[str]:
    """Rewrite a fact IN PLACE: the old wording retires into version_history, the new
    wording becomes the body. The doc id stays (stable link target). Returns [id]."""
    target = await _require_fact_in_scope(scope_root, v["fact_id"], project_id)
    target_mem = _mem_of(target)
    target_body = target.get("content") or ""
    updated = supersede_fact_mem(
        target_mem, target_body, text=v["text"], sources=_sources_of(v),
        run_id=run_id, reason=v.get("reason"), now=now,
    )
    title = v.get("title") or None
    await _persist_mem(v["fact_id"], updated, title=title)
    await _route_content(v["fact_id"], v["text"], project_id)
    return [v["fact_id"]]


_FACT_APPLIERS = {
    "new": _apply_new,
    "merge": _apply_merge,
    "supersede": _apply_supersede,
}


async def _apply_verdicts(
    verdicts: list[dict], *, project_id: str, run_id: str, scope_root: str,
    user: dict, created: list[str],
) -> list[str]:
    """Apply every verdict in batch order. Appends each `new` verdict's doc id to
    `created` (a caller-owned accumulator, populated even when a LATER verdict raises —
    that is what lets compensation delete exactly this batch's creations). Returns
    `written` — every fact-doc touched, first-touch order (the result's `facts`).

    Each verdict is independent (a fact-doc is its own unit), so there is no in-memory
    accumulation and no per-entity batching. Compensation (on a later verdict's
    failure) hard-deletes ONLY this batch's `created`; existing fact-docs modified by a
    merge or supersede are best-effort and not rolled back — the same contract the
    entity-level apply held.
    """
    now = _now()
    written: list[str] = []
    for v in verdicts:
        action = v["action"]
        if action == "skip":
            continue
        if action == "new":
            doc_id = await _apply_new(
                v, project_id=project_id, scope_root=scope_root,
                run_id=run_id, now=now, user=user,
            )
            created.append(doc_id)
            touched = [doc_id]
        else:
            touched = await _FACT_APPLIERS[action](
                v, project_id=project_id, scope_root=scope_root, run_id=run_id, now=now,
            )
        for doc_id in touched:
            if doc_id not in written:
                written.append(doc_id)
    return written


__all__ = [
    "UNKNOWN_RUN_DETAIL",
    "_resolve_run",
    "_unresolvable_fact_errors",
    "_apply_verdicts",
    "_FACT_APPLIERS",
]

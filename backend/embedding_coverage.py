"""Embedding coverage predicate — ONE home for "this document needs an embed".

see SYSTEM: embeddings — the coverage half. Both readers below import
`documents_missing_embed` from THIS module — never a re-spelled copy of its
SQL; two strings encoding the same rule is exactly the drift the `SELECT VALUE`
INVARIANT was written about:

  - routes/admin_embeddings.py `_documents_needing_embed` (the embed-missing
    sweep — what gets re-enqueued against the provider);
  - jobs/tasks/embed.py `embed_sweep_task` (the HOURLY sweep — the same
    operation on a cron, per-run capped by EMBED_SWEEP_CAP).

`embedding_status` cannot answer coverage: it DEFAULTS to 'ok', so a document
that was never embedded at all is indistinguishable from a healthy one (858/858
read 'ok' while 184 were missing in the coverage audit). The honest signal is
derived, not stored: non-empty content AND no chunks (or a failed last embed),
excluding rows that can never have chunks.

What can never have chunks is decided by the CONTENT, never by the media kind:
  - empty content after strip — an image reference (there is no OCR, so its
    content stays empty) and an audio reference that has not been transcribed
    yet both land here;
  - content with no EMBEDDABLE projection — the chunker drops transclusion
    embed nodes (pointers, not prose), so a document that is ONLY embed nodes
    yields zero chunks no matter how many times it is re-enqueued (found live
    on dev by this instrument's first reading: a 198-char doc of three
    `![…](ref:…)` nodes, forever "missing"). The check is
    `chunk_markdown(content)` itself — the ground-truth function the embedder
    runs — so the predicate can never drift from what produces chunks.
"""

from __future__ import annotations

import logging

import settings
from agent_config_seed import HELP_ID_PREFIX

from models.references import is_ref_row

logger = logging.getLogger(__name__)

# The SQL prefilter of the "needs an embed" predicate — the CANDIDATE half only.
# Chunk membership is deliberately NOT here; see EMBEDDED_IDS_SQL below.
#   - live (soft-delete filter per the house idiom);
#   - non-empty AFTER STRIP is the contract — the audit's `string::len > 20`
#     was a throwaway noise filter, not a threshold. Divergent thresholds make
#     a before/after unreadable (the instrument count is expected to be >= the
#     audit's).
# WHY: no media_type filter here — candidacy is decided by content alone.
# Why: an audio reference carries its TRANSCRIPT as content and chunks like any
# prose, so filtering it out as a "binary ref" made every consumer of this
# predicate — the admin embed-missing sweep, the hourly embed_sweep_task and the
# coverage probe — unable to ever restore a transcript whose chunks were lost
# (DB restore, failed re-embed, embedding-model change). Those documents reach
# the index ONLY through the live content_flushed path (jobs/tasks/media.py
# emits it after set_content), which applies no media filter either; measured on
# a freshly armed 124-document corpus, 54 audio transcripts were invisible here
# while a live dev had them all. Emptiness plus chunk_markdown() below already
# exclude everything that truly cannot be chunked, including the failed-status
# disjunct's worry: a failed binary ref with no content is not a candidate.
CANDIDATE_WHERE = (
    "deleted_at IS NONE "
    "AND string::trim(content ?? '') != '' "
    # The Lore guide is never embedded — see the INVARIANT in embeddings._on_content_flushed.
    f"AND !string::starts_with(meta::id(id), '{HELP_ID_PREFIX}')"
)

# WHY(perf): chunk membership is resolved by THIS standalone statement and
# folded in Python — never inlined as `NOT IN (SELECT …)` inside the documents
# scan. Why: inlined, SurrealDB re-runs the whole chunk aggregation once per
# candidate row. Measured on dev (858 docs / 427 candidates / 743 chunks): the
# inline form ran 195165ms and the driver dropped the WS response
# (`KeyError: <query id>` out of async_ws._send), taking the admin embed-missing
# sweep down with it; hoisted, the same reading is 0.9s. The audit's baseline
# query hoisted it into `LET $emb` for the same reason — a multi-statement
# db.query() cannot be used here because the driver returns only the FIRST
# statement's result.
# INVARIANT: `SELECT VALUE document_id`, never `SELECT document_id`. Why: the
# plain form yields {document_id: …} OBJECTS, which never equal an id string, so
# the membership test holds for EVERY document and any consumer of this
# predicate re-enqueues the whole corpus on every call.
EMBEDDED_IDS_SQL = "SELECT VALUE document_id FROM doc_chunks GROUP BY document_id"

# The THIRD population: document ids whose chunks sit on a model that is not
# the current one — including NONE, the "unknown, pre-column" stamp (see the
# schema comment on doc_chunks.model). $model is the caller's ONE
# EMBEDDING_MODEL settings read, threaded in — never a second get.
# WHY: `SELECT VALUE document_id`, never `SELECT document_id` — the plain form
# yields {document_id: …} objects that never equal an id string, so the stale
# population silently vanishes from the fold. Hoisted and folded in Python
# next to `embedded` for the same 195s correlated-subselect reason as
# EMBEDDED_IDS_SQL above.
STALE_MODEL_IDS_SQL = (
    "SELECT VALUE document_id FROM doc_chunks "
    "WHERE model IS NONE OR model != $model GROUP BY document_id"
)


async def chunkable_candidates(db, project_id: str | None = None) -> list[dict]:
    """`{id, project_id, is_reference, embedded, failed, stale}` per live
    document that the embedder WOULD chunk — the ONE population every
    embed-facing number is over: the sweep's "needs an embed" list and the
    admin stats numerators (see the WHY in routes/admin_embeddings.py).

    Candidates pass the SQL prefilter (CANDIDATE_WHERE), then the exact
    chunkability check (`chunk_markdown`, the function the embedder runs) — an
    embed-only document produces no chunks by design and is not in this
    population at all: not missing, not stale, never a negative. The flags are
    folded from the hoisted EMBEDDED_IDS_SQL / STALE_MODEL_IDS_SQL reads
    (`stale` = chunks on a model that is not the current one, NONE included).
    `project_id=None` spans the whole corpus.

    # DEBT: candidate bodies are materialized to run chunk_markdown over them
    # (~1MB across 427 rows on dev today). Why deferred: the exact check is what
    # keeps un-chunkable documents out of the sweep, and no cheaper predicate
    # reproduces it; revisit with a persisted chunkability flag if the corpus
    # outgrows a single scan.
    """
    from markdown_chunker import chunk_markdown

    embedded: set[str] = set(await db.query(EMBEDDED_IDS_SQL) or [])
    model = await settings.get("EMBEDDING_MODEL")
    stale: set[str] = set(
        await db.query(STALE_MODEL_IDS_SQL, {"model": model}) or []
    )

    where_project = "AND project_id = $pid " if project_id else ""
    rows = await db.query(
        "SELECT meta::id(id) AS id, project_id, content, embedding_status, "
        f"is_reference FROM documents WHERE {CANDIDATE_WHERE} {where_project}",
        {"pid": project_id} if project_id else {},
    ) or []
    return [
        {
            "id": r["id"],
            "project_id": r["project_id"],
            "is_reference": is_ref_row(r),
            "embedded": r["id"] in embedded,
            "failed": r.get("embedding_status") == "failed",
            "stale": r["id"] in stale,
        }
        for r in rows
        if chunk_markdown(r.get("content") or "")
    ]


async def documents_missing_embed(db, project_id: str | None = None) -> list[dict]:
    """`{id, project_id}` per live document that should have chunks and has
    none — or whose last embed failed (stale chunks, needs a retry) — or
    whose chunks sit on a model that is not the current one (a swapped-out
    EMBEDDING_MODEL, or NONE = "unknown, pre-column": nothing re-embeds an
    UNEDITED document, so the miss must be derived, not edited into existence).

    `project_id=None` spans the whole corpus (the historical sweep behaviour);
    a scoped call narrows to one project. The population is
    `chunkable_candidates` — see there for why the chunker check is part of it.
    """
    return [
        {"id": c["id"], "project_id": c["project_id"]}
        for c in await chunkable_candidates(db, project_id)
        if not c["embedded"] or c["failed"] or c["stale"]
    ]


def _parents_by_kind(parents: list[dict]) -> dict[str, list[str]]:
    """Group document ids by the corpus label their chunks carry — the label
    rule the direct-hit layer applies (agent/search_exec.py) and
    embeddings._chunk_kind stamps: memory if is_memory, else reference
    (strict boolean — a half-typed legacy row is a document), else document."""
    by_kind: dict[str, list[str]] = {"document": [], "reference": [], "memory": []}
    for p in parents:
        if p.get("is_memory"):
            by_kind["memory"].append(p["id"])
        elif is_ref_row(p):
            by_kind["reference"].append(p["id"])
        else:
            by_kind["document"].append(p["id"])
    return by_kind


async def backfill_doc_chunk_kind(db=None) -> int:
    """Fill `doc_chunks.kind` for rows predating the column, from the parent
    document's flags — returns how many chunk rows were updated.

    Called from the WEB lifespan right after apply_schema (main.py): the web
    DEFINES the fields at boot, so it also heals the rows that predate them;
    the worker never backfills (its first embed stamps new rows directly).

    `kind` is exactly derivable — the label rule the direct-hit layer applies
    (agent/search_exec.py: memory if is_memory, else reference, else document) —
    so one grouped UPDATE per kind heals every pre-column row. `model` is NOT
    touched: it is not derivable (a matching content_hash only recomputes under
    the CURRENT model and cannot prove which model produced the STORED vector),
    so NONE means "unknown, pre-column" and survives until the document's next
    re-embed stamps it (see the schema comment on doc_chunks.model).

    Early-exits on a corpus whose chunks all carry a kind, so steady-state
    boots pay one aggregate count. The `AND kind IS NONE` guard makes the sweep
    idempotent — it never stomps a kind a later write or reclassification set.
    `db` is injectable for tests.
    """
    if db is None:
        from db import get_db
        db = await get_db()
    rows = await db.query(
        "SELECT count() AS c FROM doc_chunks WHERE kind IS NONE GROUP ALL"
    ) or []
    if not rows or not rows[0]["c"]:
        return 0
    parents = await db.query(
        "SELECT meta::id(id) AS id, is_reference, is_memory FROM documents"
    ) or []
    updated = 0
    for kind, ids in _parents_by_kind(parents).items():
        if not ids:
            continue
        res = await db.query(
            "UPDATE doc_chunks SET kind = $k WHERE document_id IN $ids AND kind IS NONE",
            {"k": kind, "ids": ids},
        )
        updated += len(res or [])
    if updated:
        logger.info("backfill_doc_chunk_kind: filled kind on %d pre-column chunk(s)", updated)
    return updated

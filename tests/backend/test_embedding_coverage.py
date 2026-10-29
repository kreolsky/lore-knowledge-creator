"""Embedding coverage contracts — the 2026-08-15 audit fix plan.

Binds, by contract and not by fix:
  - D1: the creation PRIMITIVES (create_document / create_reference_row) emit
    content_flushed when the row is born with non-empty content; empty content
    enqueues nothing.
  - D1a: content arriving AFTER the row (save_upload → _store_markdown backfill)
    emits at the write site and ends up embedded.
  - D2: checkpoint restore with no live collab session (routed=False) emits at
    the write site.
  - D5: renaming a DOCUMENT emits content_flushed (references already do).
  - Retired-fact guard: a mem_active=false fact whose content is flushed ends
    with zero chunks (embeddings.py projection must carry mem_active).
  - D3: last_embed_error is a READABLE reason string on BOTH writers (terminal
    failure in embed_document_task; partial embed in _reembed) — not a datetime.
  - D4a: one shared needs-embed predicate — non-empty after strip, markdown/
    null media only, both disjuncts (no-chunks OR failed) — and the admin sweep
    returns the SAME set as the instrument on the same fixture.
  - Stale-model leg: chunks under another model, or NONE (pre-column), make
    their document missing — the drain population after an EMBEDDING_MODEL
    swap and after this column first deploys.
"""

from unittest.mock import patch

import pytest
import pytest_asyncio
import settings
from emit_recorder import EmitRecorder

from db import create_record, get_db

_CREATED: list[str] = []


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_seeded(test_db):
    """Remove every document/chunk row these tests seed (project-wide counters
    elsewhere must not see them)."""
    yield
    db = await get_db()
    for doc_id in _CREATED:
        await db.query("DELETE type::record('documents', $id)", {"id": doc_id})
        await db.query("DELETE doc_chunks WHERE document_id = $id", {"id": doc_id})
    _CREATED.clear()


def _flushed(emitted) -> list:
    return emitted.of("content_flushed")


async def _seed_row(doc_id: str, pid: str, *, content: str, host_id: str | None = None,
                    **extra) -> None:
    """A minimal documents row; extra fields land verbatim (media_type,
    is_reference, embedding_status, is_memory, mem_active, ...). References
    (is_reference/media_type) need a REAL host — the documents_reference_
    parent_check schema event rejects a parentless reference row."""
    db = await get_db()
    _CREATED.append(doc_id)
    await db.query("DELETE type::record('documents', $id)", {"id": doc_id})
    await db.query("DELETE doc_chunks WHERE document_id = $id", {"id": doc_id})
    row: dict = {
        "project_id": pid, "parent_id": host_id, "title": doc_id,
        "content": content, "path": f"{doc_id}.md",
        "is_reference": extra.pop("is_reference", False),
    }
    row.update(extra)
    await create_record("documents", doc_id, row)


async def _seed_chunk(doc_id: str, pid: str, *, model: str | None = None) -> None:
    """A minimal chunk row. `model=None` (the default) stamps the CURRENT
    model — a healthy chunk the stale-model leg must NOT report; the literal
    'NONE' leaves the field unset (a pre-column row, the post-deploy drain
    population); any other string lands verbatim (a swapped-out model)."""
    db = await get_db()
    extra, params = "", {}
    if model != "NONE":
        extra = ", model: $m"
        params = {"m": model or await settings.get("EMBEDDING_MODEL")}
    await db.query(
        "CREATE doc_chunks CONTENT { document_id: $id, project_id: $pid, ord: 0, "
        "content: 'text', heading: NONE, offset_start: 0, offset_end: 4, "
        f"content_version: 0, kind: 'document', embedding: [0.0]{extra} }}",
        {"id": doc_id, "pid": pid, **params},
    )


# ─── D1a: post-create backfill emits at the write site ──────────────────────


async def test_backfilled_markdown_reference_emits_flush(project_with_doc):
    """save_upload creates the reference with EMPTY content and _store_markdown
    backfills it a moment later — the flush must fire at the backfill, or the
    reference is never embedded (the likely source of the 24 unembedded md refs
    in the baseline)."""
    pid, host_id, uid = project_with_doc
    from routes.files_mcp_upload import _store_markdown

    claims = {"project_id": pid, "document_id": host_id, "user_id": uid}
    with EmitRecorder.active() as emitted:
        ref_id, _result = await _store_markdown(
            b"# Imported note\n\nSome body text worth finding.", claims,
            "note.md", "Imported note",
        )
    _CREATED.append(ref_id)
    flushed = _flushed(emitted)
    assert flushed, "backfilled markdown reference never scheduled an embed"
    assert flushed[-1]["entity_id"] == ref_id
    assert flushed[-1]["project_id"] == pid


# ─── D3: last_embed_error is a readable reason on both writers ───────────────


async def test_transient_error_exhausted_tries_dead_letters(project_with_doc):
    """A PERSISTENT transient error (503) must terminate into the dead-letter
    recorder on the final try: embedding_status='failed' + a readable reason
    naming the HTTP status AND the try count.

    Before: every attempt raised Retry; arq aborted with `job_try > max_tries`
    WITHOUT invoking the task again, so no recorder ran and the doc silently
    kept embedding_status='ok' with ZERO chunks — 187 documents on prod
    (2026-08-18), invisible to AI chat and to any sweep keyed on status.
    """
    pid, _doc_id, _uid = project_with_doc
    from jobs.tasks import embed_document_task
    from jobs.tasks.embed import EMBED_MAX_TRIES

    doc_id = "embcov-transient-exhaust-1"
    await _seed_row(doc_id, pid, content="body that keeps failing transiently")

    import httpx

    request = httpx.Request("POST", "http://embedding.test/v1/embeddings")
    response = httpx.Response(503, request=request)
    err = httpx.HTTPStatusError("Server error 503", request=request, response=response)

    import embeddings as emb
    emb._consecutive_embed_failures.clear()
    emb._degraded_projects.clear()
    try:
        with patch("embeddings._reembed", side_effect=err):
            with pytest.raises(httpx.HTTPStatusError):
                await embed_document_task(
                    {"redis": None, "job_id": f"embed:{doc_id}",
                     "job_try": EMBED_MAX_TRIES},
                    "doc", doc_id, pid,
                )
    finally:
        emb._consecutive_embed_failures.clear()
        emb._degraded_projects.clear()

    db = await get_db()
    row = (await db.query(
        "SELECT embedding_status, last_embed_error FROM documents "
        "WHERE meta::id(id) = $id",
        {"id": doc_id},
    ))[0]
    assert row["embedding_status"] == "failed", (
        "exhausted retries left the doc reading 'ok' with zero chunks"
    )
    err_text = row["last_embed_error"]
    assert isinstance(err_text, str), f"last_embed_error is not a reason string: {err_text!r}"
    assert "503" in err_text, f"reason must name the transient status: {err_text!r}"
    assert str(EMBED_MAX_TRIES) in err_text, (
        f"reason must name the try count: {err_text!r}"
    )


# ─── D4a: one shared needs-embed predicate ──────────────────────────────────


async def _seed_coverage_fixture(pid: str, host_id: str) -> set[str]:
    """The D4a fixture: every classification the predicate must make."""
    await _seed_row("embcov-fx-embedded", pid, content="embedded body")
    await _seed_chunk("embcov-fx-embedded", pid)
    await _seed_row("embcov-fx-missing", pid, content="never embedded body")
    await _seed_row("embcov-fx-empty", pid, content="   ")
    # Embed-nodes only: the chunker drops transclusion pointers (D6), so this
    # document yields zero chunks BY DESIGN — reporting it missing forever is
    # the instrument lying (found live on dev: a 198-char all-embeds doc).
    await _seed_row(
        "embcov-fx-embedonly", pid,
        content="![alt one](ref:11111111-1111-1111-1111-111111111111)\n\n"
                "![alt two](ref:22222222-2222-2222-2222-222222222222)",
    )
    await _seed_row(
        "embcov-fx-audio", pid, content="transcript text", host_id=host_id,
        media_type="audio", is_reference=True,
    )
    await _seed_row(
        "embcov-fx-mdref", pid, content="markdown reference body", host_id=host_id,
        media_type="markdown", is_reference=True,
    )
    await _seed_row(
        "embcov-fx-audio-failed", pid, content="failed audio transcript",
        host_id=host_id, media_type="audio", is_reference=True,
        embedding_status="failed",
    )
    await _seed_chunk("embcov-fx-audio-failed", pid)
    await _seed_row(
        "embcov-fx-mdref-failed", pid, content="failed markdown reference",
        host_id=host_id, media_type="markdown", is_reference=True,
        embedding_status="failed",
    )
    await _seed_chunk("embcov-fx-mdref-failed", pid)
    # An image reference can never carry text (there is no OCR pipeline), so it
    # stays out on EMPTINESS — not on its media kind. This is the row that keeps
    # the media_type filter from being reintroduced "for binary refs".
    await _seed_row(
        "embcov-fx-image", pid, content="", host_id=host_id,
        media_type="image", is_reference=True,
    )
    return {
        "embcov-fx-missing", "embcov-fx-mdref", "embcov-fx-mdref-failed",
        # Audio references carry their TRANSCRIPT as content and chunk like any
        # prose — the live content_flushed path embeds them, so the recovery
        # path must be able to see them too.
        "embcov-fx-audio", "embcov-fx-audio-failed",
    }


async def test_coverage_predicate_classifications(project_with_doc):
    """Missing = live AND non-empty-after-strip AND chunkable AND (no chunks OR
    failed) — the media kind plays no part. An empty document and an image
    reference (no OCR, so no content) can never need an embed; an audio
    reference holding a transcript can, and a failed ref of either kind is
    retried."""
    pid, host_id, _uid = project_with_doc
    from embedding_coverage import documents_missing_embed

    expected = await _seed_coverage_fixture(pid, host_id)
    rows = await documents_missing_embed(await get_db(), pid)
    assert {r["id"] for r in rows} == expected


async def test_chunk_membership_is_not_a_correlated_subselect(project_with_doc):
    """Every chunk-derived id set is resolved ONCE as a standalone statement,
    never inlined inside the documents scan.

    Binds cost, which no set-equality test can see: inlining
    `NOT IN (SELECT VALUE document_id FROM doc_chunks …)` into the documents
    WHERE makes SurrealDB re-run the chunk aggregation per candidate row.
    Measured on the dev corpus (858 docs / 427 candidates / 743 chunks): the
    inline form took 195165ms and dropped the WS response, the hoisted form
    0.9s. Assert over the SQL the function actually issues — a timing
    assertion would flake on a loaded host, which is exactly when this bites.
    """
    pid, host_id, _uid = project_with_doc
    from embedding_coverage import (
        EMBEDDED_IDS_SQL,
        STALE_MODEL_IDS_SQL,
        documents_missing_embed,
    )

    await _seed_coverage_fixture(pid, host_id)
    db = await get_db()
    issued: list[str] = []
    original = db.query

    async def _spy(sql, params=None, **kw):
        issued.append(sql)
        return await original(sql, params, **kw)

    db.query = _spy
    try:
        await documents_missing_embed(db, pid)
    finally:
        db.query = original

    correlated = [
        s for s in issued
        if "FROM documents" in s and "FROM doc_chunks" in s
    ]
    assert not correlated, f"chunk membership inlined into the documents scan: {correlated}"
    assert issued.count(EMBEDDED_IDS_SQL) == 1, (
        f"the embedded-id set must be fetched exactly once, got: {issued}"
    )
    assert issued.count(STALE_MODEL_IDS_SQL) == 1, (
        f"the stale-model set must be fetched exactly once, got: {issued}"
    )


# ─── Dead project: documents live on, candidacy must not ────────────────────


async def test_dead_project_documents_are_never_candidates(client, admin_user):
    """A deleted project's documents keep deleted_at = NONE (project delete
    marks only the project row), so the predicate must drop them by PROJECT
    liveness — otherwise the sweep and the admin coverage page count a dead
    corpus forever and re-embed it on every EMBEDDING_MODEL swap."""
    _, admin_token = admin_user
    resp = await client.post(
        "/api/projects", json={"name": "Dead Project Coverage"}, cookies={"lore_session": admin_token},
    )
    pid = resp.json()["project_id"]

    doc_id = "embcov-dead-project-1"
    await _seed_row(doc_id, pid, content="body inside what becomes a dead project")

    # False-green check: while the project is live the doc IS a candidate.
    from embedding_coverage import documents_missing_embed
    from routes.admin_embeddings import _documents_needing_embed

    db = await get_db()
    assert doc_id in {r["id"] for r in await documents_missing_embed(db, pid)}

    resp = await client.delete(f"/api/projects/{pid}", cookies={"lore_session": admin_token})
    assert resp.status_code == 200, resp.text

    # Neither the instrument (scoped and unscoped) nor the sweep may count it.
    assert doc_id not in {r["id"] for r in await documents_missing_embed(db, pid)}
    assert doc_id not in {r["id"] for r in await documents_missing_embed(db)}
    assert doc_id not in {r["id"] for r in await _documents_needing_embed(db, None)}

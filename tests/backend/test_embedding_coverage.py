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

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
import settings
from emit_recorder import EmitRecorder
from helpers import pinned_embedding_api

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


# ─── D1: creation primitives emit on non-empty content ──────────────────────


async def test_reference_primitive_with_content_emits_flush(project_with_doc):
    """create_reference_row with a body schedules the embed — asserted over the
    PRIMITIVE so the next caller added inherits it (four callers today, the
    24 unembedded markdown refs in the audit came from this gap)."""
    pid, host_id, _uid = project_with_doc
    from documents.service import create_reference_row

    with EmitRecorder.active() as emitted:
        await create_reference_row(
            ref_id="embcov-ref-prim-1", project_id=pid, host_id=host_id,
            title="T", media_type="markdown", content="# Body\nReference text",
        )
    flushed = _flushed(emitted)
    assert flushed, "reference born with content emitted no content_flushed"
    assert flushed[0]["entity_id"] == "embcov-ref-prim-1"
    assert flushed[0]["project_id"] == pid


async def test_reference_primitive_empty_content_emits_nothing(project_with_doc):
    """An empty-content create enqueues nothing — a job whose only work would be
    DELETE doc_chunks on a document that has none makes the debounce queue lie
    about how much is pending."""
    pid, host_id, _uid = project_with_doc
    from documents.service import create_reference_row

    with EmitRecorder.active() as emitted:
        await create_reference_row(
            ref_id="embcov-ref-empty-1", project_id=pid, host_id=host_id,
            title="T", media_type="markdown", content=None,
        )
    assert not _flushed(emitted)


async def test_document_primitive_with_content_emits_flush(project_with_doc):
    """create_document with a body schedules the embed (asserted over the
    primitive: REST create, seed, and memory facts all funnel through it)."""
    pid, _host_id, _uid = project_with_doc
    from documents.service import create_document

    payload = {
        "project_id": pid, "parent_id": None, "title": "T",
        "content": "A body worth indexing.", "is_reference": False,
        "path": "embcov-doc-prim-1.md",
    }
    with EmitRecorder.active() as emitted:
        await create_document("embcov-doc-prim-1", payload)
    flushed = _flushed(emitted)
    assert flushed, "document born with content emitted no content_flushed"
    assert flushed[0]["entity_id"] == "embcov-doc-prim-1"
    assert flushed[0]["project_id"] == pid


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


# ─── D5: rename re-embeds documents (references already did) ────────────────


async def test_renaming_a_document_emits_flush(client, admin_user, project_with_doc):
    """The title is inside the embedded breadcrumb and inside _chunk_hash, so a
    rename genuinely changes every vector — documents must flush like refs."""
    pid, doc_id, _uid = project_with_doc
    _, token = admin_user
    from event_bus import off, on

    caught: list[dict] = []

    async def catcher(**kwargs):
        caught.append(kwargs)

    on("content_flushed", catcher)
    try:
        resp = await client.patch(
            f"/api/documents/{doc_id}",
            json={"title": "Renamed by test"},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200, resp.text
    finally:
        off("content_flushed", catcher)
    mine = [c for c in caught if c.get("entity_id") == doc_id]
    assert mine, "document rename emitted no content_flushed (vectors go stale)"


async def test_renaming_a_reference_still_emits_flush(client, admin_user, project_with_doc):
    """References emit on title change (references.py) — regression guard for
    the symmetry the document side is being brought up to."""
    pid, doc_id, _uid = project_with_doc
    _, token = admin_user
    from event_bus import off, on

    resp = await client.post(
        "/api/references",
        json={
            "project_id": pid, "document_id": doc_id, "title": "Before rename",
            "media_type": "markdown", "content": "body",
        },
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    ref_id = resp.json()["reference_id"]
    _CREATED.append(ref_id)

    caught: list[dict] = []

    async def catcher(**kwargs):
        caught.append(kwargs)

    on("content_flushed", catcher)
    try:
        resp = await client.patch(
            f"/api/references/{ref_id}",
            json={"title": "After rename"},
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200, resp.text
    finally:
        off("content_flushed", catcher)
    assert [c for c in caught if c.get("entity_id") == ref_id], (
        "reference rename no longer emits content_flushed"
    )


# ─── D2: checkpoint restore without a session emits at the write site ───────


async def test_restore_without_collab_session_emits_flush(
    client, admin_user, project_with_doc,
):
    """The routed=False branch persists via set_content and currently emits
    nothing — the restored text is never re-embedded."""
    pid, doc_id, uid = project_with_doc
    _, token = admin_user
    from cp_store import create_checkpoint

    cp = await create_checkpoint(
        document_id=doc_id, content="Restored body text", tables_json=None,
        label="manual", comment="coverage test", created_by=uid,
    )
    cp_id = cp["checkpoint_id"]

    from event_bus import off, on

    caught: list[dict] = []

    async def catcher(**kwargs):
        caught.append(kwargs)

    on("content_flushed", catcher)
    try:
        resp = await client.post(
            f"/api/checkpoints/{cp_id}/restore",
            cookies={"lore_session": token},
        )
        assert resp.status_code == 200, resp.text
    finally:
        off("content_flushed", catcher)
    assert [c for c in caught if c.get("entity_id") == doc_id], (
        "checkpoint restore (no session) emitted no content_flushed"
    )


# ─── Retired-fact guard: projection must carry mem_active ───────────────────


async def test_retired_fact_flush_ends_with_zero_chunks(project_with_doc):
    """A RETIRED fact (soft-deleted by retirement — `deleted_at` set) whose content
    is flushed must end with zero chunks — re-embedding it would resurrect a ghost
    next to the fact that replaced it. Mechanism (R0a, DEC2 variant C): the
    `_reembed` projection's `deleted_at IS NONE` clause makes a retired fact
    unfetchable, so the flush exits at `if not parent` and nothing is ever embedded.
    The assertion pins the CONTRACT, not the mechanism — the projection clause and
    the retirement write are jointly what keep it true."""
    pid, _doc_id, _uid = project_with_doc
    import embeddings as emb

    doc_id = "embcov-retired-fact-1"
    await _seed_row(
        doc_id, pid, content="a retired fact body",
        is_memory=True, mem_active=False,
    )
    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
        {"id": doc_id},
    )

    # WHY the config pin: CI ships .env.example, where the embedding chain is
    # empty — without it _reembed exits at _ensure_config and the test passes
    # VACUOUSLY (zero chunks because nothing ran, not because the guard held).
    # The pin covers the whole EMBEDDING→AI chain (config is the settings
    # fallback bucket).
    with pinned_embedding_api(), \
         patch("embeddings.embed_texts", new=AsyncMock(return_value=[[0.1] * 4])):
        await emb._reembed("doc", doc_id, pid)

    rows = await db.query(
        "SELECT count() AS c FROM doc_chunks WHERE document_id = $id GROUP ALL",
        {"id": doc_id},
    )
    assert not (rows and rows[0]["c"]), "retired fact kept its chunks (ghost vector)"


# ─── D3: last_embed_error is a readable reason on both writers ───────────────


async def test_terminal_embed_failure_writes_readable_reason(project_with_doc):
    """The terminal branch of embed_document_task must leave a HUMAN reason in
    last_embed_error (the load-bearing half for an operator) — not a timestamp
    duplicating updated_at. The row must survive the write."""
    pid, _doc_id, _uid = project_with_doc
    from jobs.tasks import embed_document_task

    doc_id = "embcov-terminal-fail-1"
    await _seed_row(doc_id, pid, content="body that will fail to embed")

    import embeddings as emb
    emb._consecutive_embed_failures.clear()
    emb._degraded_projects.clear()
    try:
        with patch("embeddings._reembed", side_effect=RuntimeError("provider exploded")):
            with pytest.raises(RuntimeError, match="provider exploded"):
                await embed_document_task(
                    {"redis": None, "job_id": f"embed:{doc_id}", "job_try": 1},
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
    assert row["embedding_status"] == "failed"
    err = row["last_embed_error"]
    assert isinstance(err, str), f"last_embed_error is not a reason string: {err!r}"
    assert "provider exploded" in err


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


async def test_partial_embed_keeps_ok_status_with_readable_reason(project_with_doc):
    """Provider rejects ONE changed chunk, the other lands: the document is
    genuinely indexed → status 'ok' + a readable partial reason + the surviving
    chunk in place. Today the reason write hits an option<datetime> field,
    raises, and the doc flips to 'failed' via the terminal branch."""
    pid, _doc_id, _uid = project_with_doc
    import embeddings as emb

    doc_id = "embcov-partial-1"
    await _seed_row(
        doc_id, pid,
        content="## Alpha\nfirst section body\n\n## Beta\nsecond section body",
    )

    async def fake_resilient(texts):
        return ([[0.1] * 4, None], [1])

    # Same config precondition as the retired-fact test: without a non-empty
    # embedding chain, _reembed exits at _ensure_config and the row keeps its
    # birth embedding_status (this is the exact CI failure of run #1084).
    with pinned_embedding_api(), \
         patch("embeddings._embed_texts_resilient", side_effect=fake_resilient):
        await emb._reembed("doc", doc_id, pid)

    db = await get_db()
    row = (await db.query(
        "SELECT embedding_status, last_embed_error FROM documents "
        "WHERE meta::id(id) = $id",
        {"id": doc_id},
    ))[0]
    assert row["embedding_status"] == "ok"
    err = row["last_embed_error"]
    assert isinstance(err, str), f"last_embed_error is not a reason string: {err!r}"
    assert "1" in err and "reject" in err.lower()
    chunks = await db.query(
        "SELECT count() AS c FROM doc_chunks WHERE document_id = $id GROUP ALL",
        {"id": doc_id},
    )
    assert chunks and chunks[0]["c"] == 1, "the surviving chunk was lost"


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


async def test_sweep_and_instrument_return_the_same_set(project_with_doc):
    """D4a: _documents_needing_embed (the admin embed-missing sweep) and the
    coverage instrument read ONE predicate — set equality over the derived
    rule, not two hand-written counts that can drift apart."""
    pid, host_id, _uid = project_with_doc
    from embedding_coverage import documents_missing_embed
    from routes.admin_embeddings import _documents_needing_embed

    expected = await _seed_coverage_fixture(pid, host_id)
    db = await get_db()
    sweep = {r["id"] for r in await _documents_needing_embed(db, pid)}
    instrument = {r["id"] for r in await documents_missing_embed(db, pid)}
    assert sweep == instrument == expected


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


# ─── Stale-model population: chunks under another/unknown model ─────────────


async def test_stale_or_unknown_model_chunks_are_a_coverage_miss(project_with_doc):
    """Chunks under ANOTHER model — or NONE, the pre-column population every
    deployed corpus carries until its documents re-embed once — make their
    document MISSING; chunks under the CURRENT model do not.

    Why this population exists (F4): after an EMBEDDING_MODEL swap nothing
    re-embedded an unedited document — the old predicate saw only "no chunks
    or failed" — so search silently ran against vectors the new model's query
    space cannot see, forever. The sweep and POST /embed-missing drain it; a
    re-embed whose hashes still match reuses every row without a provider
    call and only stamps `model`.
    """
    pid, _host_id, _uid = project_with_doc
    from embedding_coverage import documents_missing_embed

    await _seed_row("embcov-stale-other", pid, content="embedded under a retired model")
    await _seed_chunk("embcov-stale-other", pid, model="some-retired-model")
    await _seed_row("embcov-stale-none", pid, content="embedded before the model column")
    await _seed_chunk("embcov-stale-none", pid, model="NONE")
    await _seed_row("embcov-stale-current", pid, content="embedded under the current model")
    await _seed_chunk("embcov-stale-current", pid)  # default = current model

    rows = await documents_missing_embed(await get_db(), pid)
    assert {r["id"] for r in rows} == {"embcov-stale-other", "embcov-stale-none"}


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

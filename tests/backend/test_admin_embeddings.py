"""API tests for admin embeddings endpoints — stats, embed-missing, reset, status."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
import settings
from enqueue_recorder import EnqueueRecorder

from db import get_db


async def _cancel_running_task():
    """Cancel any running background embed task so the next test can start fresh."""
    import routes.admin_embeddings as mod
    task = mod._embed_task
    if task and not task.done():
        try:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        except RuntimeError:
            pass
    mod._embed_task = None
    mod._embed_progress = {"running": False, "operation": "embedding", "processed": 0, "total": 0, "errors": 0}


@pytest.mark.asyncio
async def test_stats_empty(client, admin_user, project_with_doc):
    """Fresh project → all embedding counts are zero."""
    _, token = admin_user
    resp = await client.get(
        "/api/admin/embeddings/stats",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["doc_chunks_count"] == 0
    assert "docs_without_embeddings" in data
    assert "refs_without_embeddings" in data


@pytest.mark.asyncio
async def test_stats_pending_reembed_counts_dead_lettered_docs(
    client, admin_user, project_with_doc,
):
    """pending_reembed_count tracks embedding_status='failed', it is not a constant.

    Binds the served number to DB state in BOTH directions: the stub it replaced
    returned 0 and so passed any assertion that only checked the healthy case.
    """
    _, token = admin_user
    _, idx_id, _ = project_with_doc
    db = await get_db()

    async def _stats() -> dict:
        resp = await client.get(
            "/api/admin/embeddings/stats", cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        return resp.json()

    assert (await _stats())["pending_reembed_count"] == 0

    await db.query(
        "UPDATE type::record('documents', $id) SET "
        "embedding_status = 'failed', last_embed_error = 'embed failed: repro'",
        {"id": idx_id},
    )
    assert (await _stats())["pending_reembed_count"] == 1

    await db.query(
        "UPDATE type::record('documents', $id) SET "
        "embedding_status = 'ok', last_embed_error = NONE",
        {"id": idx_id},
    )
    assert (await _stats())["pending_reembed_count"] == 0


@pytest.mark.asyncio
async def test_stats_pending_reembed_skips_dead_project(
    client, admin_user, project_with_doc,
):
    """A failed doc in a DELETED project leaves pending_reembed_count: project
    delete keeps the doc's deleted_at = NONE and the sweep skips it, so counting
    it would leave a number nothing can drain."""
    _, token = admin_user
    pid, idx_id, _ = project_with_doc
    db = await get_db()

    async def _pending() -> int:
        resp = await client.get(
            "/api/admin/embeddings/stats", cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        return resp.json()["pending_reembed_count"]

    await db.query(
        "UPDATE type::record('documents', $id) SET "
        "embedding_status = 'failed', last_embed_error = 'embed failed: repro'",
        {"id": idx_id},
    )
    # False-green check: counted while the project is live.
    before = await _pending()
    assert before >= 1

    resp = await client.delete(f"/api/projects/{pid}", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    assert await _pending() == before - 1


@pytest.mark.asyncio
async def test_stats_chunked_audio_ref_is_never_negative(
    client, admin_user, project_with_doc,
):
    """A chunked audio transcript keeps refs_without_embeddings at 0, never negative.

    # INVARIANT: the refs numerator is a SUBSET of the refs denominator — both
    # filter through embedding_coverage.CANDIDATE_WHERE. Why: refs_total used to
    # filter media_type = 'markdown' while refs_with_emb_count counted every
    # chunk-owning reference (audio refs chunk their transcript), so a single
    # chunked audio ref read refs_without_embeddings = -1. The subset property
    # must hold by construction, not by likely data.
    """
    pid, idx_id, _ = project_with_doc
    await _seed_ref(pid, idx_id, "stats-audio-chunked-1", media_type="audio",
                    content="chunked transcript text", with_chunk=True)

    resp = await client.get(
        "/api/admin/embeddings/stats", cookies={"lore_session": admin_user[1]},
    )

    assert resp.status_code == 200
    assert resp.json()["refs_without_embeddings"] == 0


@pytest.mark.asyncio
async def test_stats_markdown_ref_without_chunks_counts_one(
    client, admin_user, project_with_doc,
):
    """A markdown reference with content and no chunks is exactly the one missing ref."""
    pid, idx_id, _ = project_with_doc
    await _seed_ref(pid, idx_id, "stats-mdref-1", media_type="markdown",
                    content="unembedded reference body")

    resp = await client.get(
        "/api/admin/embeddings/stats", cookies={"lore_session": admin_user[1]},
    )

    assert resp.status_code == 200
    assert resp.json()["refs_without_embeddings"] == 1


@pytest.mark.asyncio
async def test_stats_emptied_chunked_doc_counted_on_neither_side(
    client, admin_user, project_with_doc,
):
    """A document with chunks but empty content is on NEITHER stats side.

    # INVARIANT: the content guard (CANDIDATE_WHERE) is on docs_total AND on the
    # numerator's parent lookup. Why: with the guard on the denominator only, a
    # chunked document whose content was later emptied stayed in the numerator
    # while leaving the denominator — the same negative-count defect the media
    # filter caused on the refs side, one rename away.
    """
    pid, _idx, _ = project_with_doc
    await _seed_doc(pid, "stats-emptied-1", content="", with_chunk=True)

    resp = await client.get(
        "/api/admin/embeddings/stats", cookies={"lore_session": admin_user[1]},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["doc_chunks_count"] == 1
    assert data["docs_without_embeddings"] == 0


@pytest.mark.asyncio
async def test_stats_stale_model_count(client, admin_user, project_with_doc):
    """stale_model_count tracks documents whose chunks sit on another or
    unknown model — BOTH directions, like pending_reembed_count's test: a stub
    returning 0 passes any healthy-only assertion, and this is the number the
    operator watches drain after an EMBEDDING_MODEL swap."""
    _, token = admin_user
    pid, _idx, _ = project_with_doc

    async def _stats() -> dict:
        resp = await client.get(
            "/api/admin/embeddings/stats", cookies={"lore_session": token},
        )
        assert resp.status_code == 200
        return resp.json()

    assert (await _stats())["stale_model_count"] == 0

    await _seed_doc(pid, "stats-stale-other-1", with_chunk=True,
                    chunk_model="some-retired-model")
    await _seed_doc(pid, "stats-stale-none-1", with_chunk=True, chunk_model="NONE")
    await _seed_doc(pid, "stats-stale-current-1", with_chunk=True)  # current model

    assert (await _stats())["stale_model_count"] == 2, (
        "stale-model docs (other + NONE) must be counted; the current-model doc must not"
    )


_EMBED_ONLY = "\n![a](ref:0d3d8db4-bb6c-494f-b4a3-943bd5ae7425)\n\n![b](ref:4687f19d-54a7-4d2e-a811-04c86a412ce0)\n"


@pytest.mark.asyncio
async def test_stats_counts_only_what_embed_missing_would_enqueue(
    client, admin_user, project_with_doc,
):
    """Every /stats numerator is over the SAME population /embed-missing
    enqueues — chunkable candidates — so a red number can always be drained.

    # INVARIANT: an embed-only document (non-empty content, zero chunks from
    # chunk_markdown) is on NO stats side — not "without embeddings", not
    # "stale model", and never a negative. Why: stats used to count it while the
    # sweep skipped it, so the operator saw "stale: 1" and "Enqueued: 0" from the
    # same button, a number nothing could ever drain (seen live on dev: a
    # 198-char doc of three ref nodes and a four-image doc on a NONE-model chunk).
    """
    _, token = admin_user
    pid, _idx, _ = project_with_doc
    from markdown_chunker import chunk_markdown
    assert chunk_markdown(
        _EMBED_ONLY, max_chunk_chars=3000, input_max_chars=8000,
    ) == [], "fixture must be embed-only"

    await _seed_doc(pid, "stats-embedonly-nochunks", content=_EMBED_ONLY)
    await _seed_doc(pid, "stats-embedonly-stale", content=_EMBED_ONLY,
                    with_chunk=True, chunk_model="NONE")
    await _seed_doc(pid, "stats-prose-stale", with_chunk=True, chunk_model="NONE")
    await _seed_doc(pid, "stats-prose-missing")

    resp = await client.get(
        "/api/admin/embeddings/stats", cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["docs_without_embeddings"] == 1, data
    assert data["stale_model_count"] == 1, data

    from embedding_coverage import documents_missing_embed
    sweep = {r["id"] for r in await documents_missing_embed(await get_db())}
    assert sweep == {"stats-prose-stale", "stats-prose-missing"}
    assert len(sweep) == data["docs_without_embeddings"] + data["stale_model_count"]


@pytest.mark.asyncio
async def test_embed_missing_starts_background(client, admin_user, project_with_doc):
    """POST /embed-missing enqueues embed_document_task per missing doc."""
    await _cancel_running_task()
    _, token = admin_user
    with patch("embeddings._ensure_config", new_callable=AsyncMock), \
         EnqueueRecorder.active():
        resp = await client.post(
            "/api/admin/embeddings/embed-missing",
            json={},
            cookies={"lore_session": token},
        )
    assert resp.status_code == 200
    assert resp.json()["started"] is True


_SEEDED: list[str] = []


@pytest_asyncio.fixture(autouse=True)
async def _drop_seeded_docs(test_db):
    """Remove every doc/chunk `_seed_doc` created, after each test.

    # INVARIANT: a test that seeds a doc_chunk MUST delete it again.
    # Why: `/stats` counts chunks PROJECT-WIDE, so a leftover chunk makes
    # `test_stats_empty` ("fresh project → all counts zero") fail depending on which
    # test ran first — a real order-dependent flake, caught only because the two
    # embed-missing tests happened to run before it in a narrowed selection.
    """
    yield
    db = await get_db()
    for doc_id in _SEEDED:
        await db.query("DELETE type::record('documents', $id)", {"id": doc_id})
        await db.query("DELETE doc_chunks WHERE document_id = $id", {"id": doc_id})
    _SEEDED.clear()


async def _chunk_model_clause(chunk_model: str | None) -> tuple[str, dict]:
    """', model: $m' + params for a seeded chunk. `chunk_model=None` (the
    default) stamps the CURRENT model — a healthy chunk the sweep's
    stale-model leg must NOT report; the literal 'NONE' leaves the field
    unset (a pre-column row); any other string lands verbatim (a swapped-out
    model)."""
    if chunk_model == "NONE":
        return "", {}
    return ", model: $m", {"m": chunk_model or await settings.get("EMBEDDING_MODEL")}


async def _seed_doc(pid: str, doc_id: str, *, status: str | None = None,
                    with_chunk: bool = False, content: str = "text",
                    chunk_model: str | None = None) -> None:
    """A document in `pid`, optionally already chunked and/or `embedding_status`."""
    from db import create_record

    db = await get_db()
    _SEEDED.append(doc_id)
    await db.query("DELETE type::record('documents', $id)", {"id": doc_id})
    await db.query("DELETE doc_chunks WHERE document_id = $id", {"id": doc_id})
    data: dict = {"project_id": pid, "parent_id": None, "title": doc_id,
                  "content": content, "path": f"{doc_id}.md"}
    if status:
        data["embedding_status"] = status
    await create_record("documents", doc_id, data)
    if with_chunk:
        extra, params = await _chunk_model_clause(chunk_model)
        await db.query(
            "CREATE doc_chunks CONTENT { document_id: $id, project_id: $pid, ord: 0, "
            "content: 'text', offset_start: 0, offset_end: 4, content_version: 0, "
            f"kind: 'document', embedding: [0.0]{extra} }}",
            {"id": doc_id, "pid": pid, **params},
        )


async def _seed_ref(pid: str, host_id: str, ref_id: str, *, media_type: str,
                    content: str, with_chunk: bool = False) -> None:
    """A reference row (is_reference=true) under `host_id`, optionally chunked."""
    from db import create_record

    db = await get_db()
    _SEEDED.append(ref_id)
    await db.query("DELETE type::record('documents', $id)", {"id": ref_id})
    await db.query("DELETE doc_chunks WHERE document_id = $id", {"id": ref_id})
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": host_id, "title": ref_id,
        "content": content, "path": f"_ref/{ref_id}.md",
        "is_reference": True, "media_type": media_type,
    })
    if with_chunk:
        extra, params = await _chunk_model_clause(None)
        await db.query(
            "CREATE doc_chunks CONTENT { document_id: $id, project_id: $pid, ord: 0, "
            "content: 'text', offset_start: 0, offset_end: 4, content_version: 0, "
            f"kind: 'reference', embedding: [0.0]{extra} }}",
            {"id": ref_id, "pid": pid, **params},
        )


async def _embed_missing(client, token: str, body: dict) -> list[str]:
    """POST /embed-missing with `body`; return the entity ids it enqueued."""
    with patch("embeddings._ensure_config", new_callable=AsyncMock), \
         EnqueueRecorder.active() as enq:
        resp = await client.post(
            "/api/admin/embeddings/embed-missing",
            json=body,
            cookies={"lore_session": token},
        )
    assert resp.status_code == 200, resp.text
    return [c.args[1] for c in enq.calls]


@pytest.mark.asyncio
async def test_embed_missing_scoped_to_one_project(client, admin_user, project_with_doc):
    """`project_id` narrows the sweep to ONE project.

    Without it the endpoint enqueues every unchunked document in EVERY project — a
    full-corpus burst against the embedding provider, which is why running it on prod
    was a deliberate act rather than a routine repair (plan 1785964800000, Backfill).
    The filter is what makes "re-embed this project" an ordinary operation.
    """
    await _cancel_running_task()
    pid, _idx, _uid = project_with_doc
    await _seed_doc(pid, "embmiss-mine-1")
    await _seed_doc("embmiss-other-project", "embmiss-theirs-1")

    ids = await _embed_missing(client, admin_user[1], {"project_id": pid})

    assert "embmiss-mine-1" in ids
    assert "embmiss-theirs-1" not in ids, "the sweep escaped the requested project"


@pytest.mark.asyncio
async def test_embed_missing_retries_a_failed_document(client, admin_user, project_with_doc):
    """A document whose last embed FAILED is re-enqueued even though it has chunks.

    `embedding_status='failed'` was a dead end: the sweep keyed on "has no chunks", so a
    provider 502 (two entities in the 2026-08-04 consolidation run) left the document
    marked failed forever with stale chunks and nothing to pick it up. Retrying is the
    whole point of recording the status.
    """
    await _cancel_running_task()
    pid, _idx, _uid = project_with_doc
    await _seed_doc(pid, "embmiss-failed-1", status="failed", with_chunk=True)
    await _seed_doc(pid, "embmiss-ok-1", status="ok", with_chunk=True)

    ids = await _embed_missing(client, admin_user[1], {"project_id": pid})

    assert "embmiss-failed-1" in ids, "a failed document is never retried"
    assert "embmiss-ok-1" not in ids, "an embedded, healthy document was re-embedded"


@pytest.mark.asyncio
async def test_status_during_embed(client, admin_user, project_with_doc):
    """Status endpoint returns running state."""
    _, token = admin_user
    resp = await client.get(
        "/api/admin/embeddings/status",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "running" in data
    assert "processed" in data
    assert "total" in data
    assert "operation" in data


async def _await_reset_done(timeout: float = 10) -> None:
    """Wait for the reset background task to finish (it swallows batch errors).

    Shield so a wait_for timeout cancels the wait, not the task itself.
    """
    import routes.admin_embeddings as mod

    task = mod._embed_task
    if task and not task.done():
        await asyncio.wait_for(asyncio.shield(task), timeout)


@pytest.mark.asyncio
async def test_reset_docs(client, admin_user, project_with_doc):
    """Reset deletes every doc_chunks row — the COUNT reaching 0 is the acceptance.

    # INVARIANT: reset's batch DELETE matches rows through meta::id(id) against
    # bare ids. Why: SurrealDB never equates a record id with its string form,
    # so a string-form predicate still answers {started: true} while deleting
    # nothing — {started} alone is never acceptance for reset.
    # Kept meaningful by test_reset_string_form_predicate_deletes_nothing.
    """
    await _cancel_running_task()
    pid, _idx, _uid = project_with_doc
    await _seed_doc(pid, "reset-docs-1", with_chunk=True)
    db = await get_db()
    rows = await db.query("SELECT count() AS c FROM doc_chunks GROUP ALL")
    assert rows and rows[0]["c"] >= 1, "seeded chunk is missing before reset"

    resp = await client.post(
        "/api/admin/embeddings/reset-docs",
        json={},
        cookies={"lore_session": admin_user[1]},
    )
    assert resp.status_code == 200
    assert resp.json()["started"] is True
    await _await_reset_done()

    rows = await db.query("SELECT count() AS c FROM doc_chunks GROUP ALL")
    assert (rows[0]["c"] if rows else 0) == 0, "reset left chunks behind"


@pytest.mark.asyncio
async def test_reset_string_form_predicate_deletes_nothing(project_with_doc):
    """The string-form DELETE ('doc_chunks:<id>' strings vs record ids) leaves the
    chunk — the falsifier proving test_reset_docs discriminates the predicates.

    # WHY: SurrealDB never equating a record id with its string form is the ONLY
    # property separating the working reset predicate from the broken one. If the
    # string form ever started deleting, test_reset_docs would pass with either
    # predicate and pin nothing.
    """
    pid, _idx, _uid = project_with_doc
    await _seed_doc(pid, "reset-stringform-1", with_chunk=True)
    db = await get_db()
    rows = await db.query(
        "SELECT meta::id(id) AS id FROM doc_chunks WHERE document_id = $id",
        {"id": "reset-stringform-1"},
    )
    assert rows, "seeded chunk is missing before the falsifier runs"
    string_ids = [f"doc_chunks:{r['id']}" for r in rows]
    await db.query("DELETE FROM doc_chunks WHERE id IN $ids", {"ids": string_ids})
    left = await db.query(
        "SELECT count() AS c FROM doc_chunks WHERE document_id = $id GROUP ALL",
        {"id": "reset-stringform-1"},
    )
    assert (left[0]["c"] if left else 0) == 1, (
        "the string-form predicate deleted the chunk — reset's count assertion "
        "no longer discriminates the two predicate forms"
    )


@pytest.mark.asyncio
async def test_reset_refs_endpoint_removed(client, admin_user, project_with_doc):
    """reset-refs endpoint removed: ref_embeddings table is gone (unified into doc_chunks)."""
    _, token = admin_user
    resp = await client.post(
        "/api/admin/embeddings/reset-refs",
        json={},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reset_docs_progress(client, admin_user, project_with_doc):
    """Reset docs shows progress via status endpoint."""
    await _cancel_running_task()
    _, token = admin_user
    resp = await client.post(
        "/api/admin/embeddings/reset-docs",
        json={},
        cookies={"lore_session": token},
    )
    assert resp.json().get("started") or resp.json().get("running") is not False
    await asyncio.sleep(0.5)
    resp = await client.get(
        "/api/admin/embeddings/status",
        cookies={"lore_session": token},
    )
    data = resp.json()
    assert data["operation"] == "reset_docs"


@pytest.mark.asyncio
async def test_stats_requires_admin(client, regular_user, project_with_doc):
    """Stats endpoint returns 403 for regular user."""
    _, token = regular_user
    resp = await client.get(
        "/api/admin/embeddings/stats",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 403


# ─── chat-recall index ───────────────────────────────────────────────────────


@pytest.fixture
def chat_embed(monkeypatch):
    """Embeddings configured, the provider faked at its boundary."""
    from helpers import fake_embedding_provider
    return fake_embedding_provider(monkeypatch, lambda _: [0.5] * 8)


@pytest_asyncio.fixture
async def chat_corpus(test_db):
    """Seeds chat sessions/messages; deletes every chat row again on teardown.

    # INVARIANT: a test that seeds chat rows MUST delete them again.
    # Why: `chat.chunks` and the pending sums are corpus-wide, like doc_chunks.
    """
    async def _seed(sid, uid, pid, messages, **fields):
        await test_db.query(
            "CREATE type::record('chat_sessions', $id) CONTENT $f",
            {"id": sid, "f": {"user_id": uid, "project_id": pid, "title": sid,
                              "model": "m", **fields}},
        )
        for mid, content in messages:
            await test_db.query(
                "CREATE type::record('messages', $id) CONTENT $f",
                {"id": mid, "f": {"chat_id": sid, "role": "user", "content": content}},
            )
    yield _seed
    await test_db.query(
        "DELETE chat_chunks; DELETE messages; DELETE chat_sessions; DELETE instance_skills")


async def _chat_stats(client, token) -> dict:
    resp = await client.get("/api/admin/embeddings/stats", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json()["chat"]


async def _index_chats(client, token):
    with EnqueueRecorder.active() as enq:
        resp = await client.post(
            "/api/admin/embeddings/index-chats", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json(), enq.of("index_chat_messages")


@pytest.mark.asyncio
async def test_chat_stats_drain_after_the_pair_task(client, admin_user, chat_embed, chat_corpus):
    """Two unindexed messages read pending 2; the pair task drains them to 0."""
    from jobs.tasks.chat_index import index_chat_messages
    _, token = admin_user
    await chat_corpus("cs1", "u1", "p1", [("cm1", "the gate"), ("cm2", "the tower")])
    before = await _chat_stats(client, token)
    assert before == {"state": "on", "chunks": 0, "pending_messages": 2, "stale_chunks": 0}
    await index_chat_messages({}, "u1", "p1")
    after = await _chat_stats(client, token)
    assert after["pending_messages"] == 0 and after["stale_chunks"] == 0
    assert after["chunks"] > 0


@pytest.mark.asyncio
async def test_chat_stats_equal_what_index_chats_posts(
    client, admin_user, chat_embed, chat_corpus,
):
    """pending is summed over exactly the pairs /index-chats posts; an indexed
    pair is posted for nothing and running the posted pairs drains it to 0."""
    from jobs.tasks.chat_index import index_chat_messages
    _, token = admin_user
    await chat_corpus("done", "u-done", "p1", [("d1", "already indexed")])
    await index_chat_messages({}, "u-done", "p1")
    await chat_corpus("lag1", "u-a", "p1", [("a1", "one"), ("a2", "two")])
    await chat_corpus("lag2", "u-b", "p2", [("b1", "three")])

    stats = await _chat_stats(client, token)
    body, posts = await _index_chats(client, token)
    assert body == {"started": True, "count": len(posts)}
    assert {(c.args, c.kwargs["job_id"]) for c in posts} == {
        (("u-a", "p1"), "chat_index:u-a:p1"), (("u-b", "p2"), "chat_index:u-b:p2"),
    }
    assert stats["pending_messages"] == 3

    for c in posts:
        await index_chat_messages({}, *c.args)
    drained = await _chat_stats(client, token)
    assert (drained["pending_messages"], drained["stale_chunks"]) == (0, 0)
    assert (await _index_chats(client, token))[0] == {"started": True, "count": 0}


@pytest.mark.asyncio
async def test_chat_stats_other_model_chunk_is_pending_and_stale(
    client, admin_user, chat_embed, chat_corpus, test_db,
):
    from jobs.tasks.chat_index import index_chat_messages
    _, token = admin_user
    await chat_corpus("cs1", "u1", "p1", [("cm1", "old geometry")])
    await index_chat_messages({}, "u1", "p1")
    chunks = (await _chat_stats(client, token))["chunks"]
    assert chunks > 0
    await test_db.query("UPDATE chat_chunks SET model = 'retired/model'")
    stats = await _chat_stats(client, token)
    assert (stats["pending_messages"], stats["stale_chunks"]) == (1, chunks)


@pytest.mark.parametrize("fields", [
    {"is_note": True}, {"unindexed": True}, {"deleted_at": "now"},
], ids=["note", "unindexed", "deleted"])
@pytest.mark.asyncio
async def test_chat_stats_skip_unindexable_chats(
    client, admin_user, chat_embed, chat_corpus, test_db, fields,
):
    _, token = admin_user
    await chat_corpus("cs1", "u1", "p1", [("cm1", "not for the index")])
    # False-green check: the same chat counts while it is indexable.
    assert (await _chat_stats(client, token))["pending_messages"] == 1
    if "deleted_at" in fields:
        await test_db.query(
            "UPDATE type::record('chat_sessions', 'cs1') SET deleted_at = time::now()")
    else:
        await test_db.query(
            "UPDATE type::record('chat_sessions', 'cs1') MERGE $f", {"f": fields})
    assert (await _chat_stats(client, token))["pending_messages"] == 0


@pytest.mark.asyncio
async def test_chat_stats_skill_disabled(client, admin_user, chat_embed, chat_corpus, test_db):
    """Disabled skill: numbers are None (never 0) and /index-chats posts nothing."""
    _, token = admin_user
    await chat_corpus("cs1", "u1", "p1", [("cm1", "pending while disabled")])
    await test_db.query(
        "CREATE instance_skills CONTENT {name: 'chat-recall', enabled: false, updated_by: 'admin'}")
    stats = await _chat_stats(client, token)
    assert stats == {"state": "skill_disabled", "chunks": 0,
                     "pending_messages": None, "stale_chunks": None}
    body, posts = await _index_chats(client, token)
    assert body == {"started": False, "error": "skill_disabled"}
    assert posts == []


@pytest.mark.asyncio
async def test_chat_stats_embeddings_unconfigured(client, admin_user, chat_embed, chat_corpus):
    import embeddings
    _, token = admin_user
    await chat_corpus("cs1", "u1", "p1", [("cm1", "pending, no provider")])
    embeddings._ensure_config.side_effect = embeddings.EmbeddingConfigError("no url")
    stats = await _chat_stats(client, token)
    assert stats["state"] == "embeddings_unconfigured"
    assert (stats["pending_messages"], stats["stale_chunks"]) == (None, None)
    body, posts = await _index_chats(client, token)
    assert body == {"started": False, "error": "embeddings_unconfigured"}
    assert posts == []


@pytest.mark.asyncio
async def test_index_chats_requires_admin(client, regular_user, chat_corpus):
    _, token = regular_user
    await chat_corpus("cs1", "u1", "p1", [("cm1", "someone's chat")])
    with EnqueueRecorder.active() as enq:
        resp = await client.post(
            "/api/admin/embeddings/index-chats", cookies={"lore_session": token})
    assert resp.status_code == 403
    assert enq.calls == []

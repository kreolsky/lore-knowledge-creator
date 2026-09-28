"""embed_sweep_task — the hourly coverage sweep (the cron half of /embed-missing).

The sweep is the SAME operation as POST /embed-missing on a schedule: it calls
the ONE shared predicate (embedding_coverage.documents_missing_embed) and
enqueues embed_document_task per missing document with the stable job_id
embed:{id} — arq dedup collapses a cron enqueue onto a pending user-edit
enqueue (the existing debounce contract). The per-run cap drains a cold corpus
(222 docs on prod, 2026-08-18) over several hourly cycles instead of one
provider burst.
"""

from unittest.mock import AsyncMock, patch

import pytest_asyncio
import settings

from db import create_record, get_db

_SEEDED: list[str] = []


@pytest_asyncio.fixture(autouse=True)
async def _drop_seeded(test_db):
    """Remove every doc/chunk `_seed_doc` created, after each test.

    # INVARIANT: a test that seeds a doc_chunk MUST delete it again.
    # Why: the sweep is CORPUS-WIDE (project_id=None), so a leftover row in any
    # project changes what a later test's derived expectation captures.
    """
    yield
    db = await get_db()
    for doc_id in _SEEDED:
        await db.query("DELETE type::record('documents', $id)", {"id": doc_id})
        await db.query("DELETE doc_chunks WHERE document_id = $id", {"id": doc_id})
    _SEEDED.clear()


async def _seed_doc(pid: str, doc_id: str, *, status: str | None = None,
                    with_chunk: bool = False,
                    chunk_model: str | None = None) -> None:
    """`chunk_model=None` stamps the CURRENT model — a healthy chunk the sweep
    must NOT re-enqueue; the literal 'NONE' leaves it unset (pre-column); any
    other string lands verbatim (a swapped-out model)."""
    db = await get_db()
    _SEEDED.append(doc_id)
    await db.query("DELETE type::record('documents', $id)", {"id": doc_id})
    await db.query("DELETE doc_chunks WHERE document_id = $id", {"id": doc_id})
    data: dict = {"project_id": pid, "parent_id": None, "title": doc_id,
                  "content": f"body of {doc_id}", "path": f"{doc_id}.md"}
    if status:
        data["embedding_status"] = status
    await create_record("documents", doc_id, data)
    if with_chunk:
        extra, params = "", {}
        if chunk_model != "NONE":
            extra = ", model: $m"
            params = {"m": chunk_model or await settings.get("EMBEDDING_MODEL")}
        await db.query(
            "CREATE doc_chunks CONTENT { document_id: $id, project_id: $pid, ord: 0, "
            "content: 'text', offset_start: 0, offset_end: 4, content_version: 0, "
            f"kind: 'document', embedding: [0.0]{extra} }}",
            {"id": doc_id, "pid": pid, **params},
        )


async def test_sweep_enqueues_exactly_the_predicates_rows(
    project_with_doc, enqueue_recorder,
):
    """The sweep enqueues exactly what documents_missing_embed returns — no
    second implementation of the rule, no extra filtering. Anchored against a
    hardcoded classification set (missing + failed-with-chunks + stale-model
    in, embedded and empty out) AND cross-checked with the predicate derived
    in-test."""
    pid, _idx, _uid = project_with_doc
    from embedding_coverage import documents_missing_embed

    from jobs.tasks.embed import embed_sweep_task

    await _seed_doc(pid, "emb-sweep-missing-1")
    await _seed_doc(pid, "emb-sweep-missing-2")
    await _seed_doc(pid, "emb-sweep-embedded-1", with_chunk=True)
    await _seed_doc(pid, "emb-sweep-failed-1", status="failed", with_chunk=True)
    await _seed_doc(pid, "emb-sweep-stale-1", with_chunk=True,
                    chunk_model="some-retired-model")
    await _seed_doc(pid, "emb-sweep-stale-none-1", with_chunk=True,
                    chunk_model="NONE")

    with patch("embeddings._ensure_config", new_callable=AsyncMock):
        await embed_sweep_task({"redis": None})

    calls = enqueue_recorder.of("embed_document_task")
    enqueued_ids = {c.args[1] for c in calls}
    expected = {
        "emb-sweep-missing-1", "emb-sweep-missing-2", "emb-sweep-failed-1",
        "emb-sweep-stale-1", "emb-sweep-stale-none-1",
    }
    derived = {r["id"] for r in await documents_missing_embed(await get_db())}
    assert enqueued_ids == expected
    assert derived == expected, "predicate drifted from the seeded classification"
    for c in calls:
        assert c.args[0] == "doc"
        assert c.kwargs["job_id"] == f"embed:{c.args[1]}", (
            "the sweep MUST reuse the stable per-doc job_id — arq dedup is what "
            "collapses a cron enqueue onto a pending user-edit enqueue"
        )


async def test_sweep_caps_enqueues_per_run(project_with_doc, monkeypatch, enqueue_recorder):
    """The per-run cap bounds one sweep to EMBED_SWEEP_CAP enqueues so a cold
    corpus drains over several hourly cycles instead of one provider burst."""
    pid, _idx, _uid = project_with_doc
    from jobs.tasks import embed_sweep_task

    monkeypatch.setattr("jobs.tasks.embed.EMBED_SWEEP_CAP", 2)
    await _seed_doc(pid, "emb-sweep-cap-1")
    await _seed_doc(pid, "emb-sweep-cap-2")
    await _seed_doc(pid, "emb-sweep-cap-3")

    with patch("embeddings._ensure_config", new_callable=AsyncMock):
        await embed_sweep_task({"redis": None})

    assert len(enqueue_recorder.of("embed_document_task")) == 2


async def test_sweep_skips_when_embedding_not_configured(enqueue_recorder):
    """No EMBEDDING_API_URL → the sweep enqueues NOTHING.

    Why: each enqueued task would run _reembed, exit at the config check, and
    burn a worker slot every hour on an unconfigured instance — and if the
    config guard ever moved into the failure path, it would dead-letter the
    whole corpus as 'failed' on a box that merely lacks a provider."""
    from embeddings import EmbeddingConfigError
    from jobs.tasks import embed_sweep_task

    with patch("embeddings._ensure_config", new_callable=AsyncMock,
               side_effect=EmbeddingConfigError("EMBEDDING_API_URL is not configured")):
        await embed_sweep_task({"redis": None})

    assert not enqueue_recorder.calls


def test_sweep_registered_on_default_worker_and_hourly_cron():
    """embed_sweep_task is registered on WorkerSettings (manually enqueueable —
    the prod backfill path) AND on an hourly cron slot. Singleton across
    replicas comes from arq's cron lock — the pattern of the other maintenance
    crons (worker.py)."""
    from jobs.worker import WorkerSettings

    fns = {getattr(f, "name", None): f for f in WorkerSettings.functions}
    assert "embed_sweep_task" in fns, (
        "not registered in WorkerSettings.functions — an unregistered task "
        "enqueues and silently never runs (arq drops it)"
    )
    assert fns["embed_sweep_task"].max_tries == 1

    crons = {c.name: c for c in WorkerSettings.cron_jobs}
    c = crons.get("cron:embed_sweep_task")
    assert c is not None, "embed_sweep_task missing from WorkerSettings.cron_jobs"
    assert c.minute == 10, "hourly at :10 — offset from the :00/:15/:45 maintenance slots"
    assert c.hour is None, "every hour"
    assert c.max_tries == 1


async def test_cap_counts_landed_enqueues_not_dedup_collapses(
    project_with_doc, monkeypatch,
):
    """A dedup-collapsed enqueue must NOT consume the per-run cap.

    Why: the cap exists to bound live PROVIDER load, and a job_id that arq
    collapses onto an already-pending job produces none — that is the debounce
    contract firing, not work. Counting attempts instead made a sweep running
    during an editing burst drain a fraction of its budget while logging a
    full one.
    """
    pid, _idx, _uid = project_with_doc
    from jobs.tasks.embed import embed_sweep_task

    monkeypatch.setattr("jobs.tasks.embed.EMBED_SWEEP_CAP", 2)
    for n in (1, 2, 3):
        await _seed_doc(pid, f"emb-sweep-collapse-{n}")

    attempts: list[str] = []
    landed: list[str] = []

    async def fake_enqueue(name, *args, **kwargs):
        attempts.append(args[1])
        if len(attempts) == 1:
            return None  # arq returns None when the job_id is already pending
        landed.append(args[1])
        return object()

    monkeypatch.setattr("jobs.pool.enqueue", fake_enqueue)

    with patch("embeddings._ensure_config", new_callable=AsyncMock):
        await embed_sweep_task({"redis": None})

    assert len(landed) == 2, (
        "a collapsed enqueue consumed cap budget it produced no provider load for"
    )
    assert len(attempts) == 3


async def test_sweep_does_not_pin_the_same_head_every_run(
    project_with_doc, monkeypatch, enqueue_recorder,
):
    """With more missing docs than the cap, repeated runs must not select the
    SAME slice every time.

    Why: the sweep has no per-document attempt cap by design, so a document
    that keeps failing stays in the predicate's set forever. Under a fixed
    scan order a persistently failing head would hold the whole cap every
    hour and everything past it would never be swept at all.
    """
    pid, _idx, _uid = project_with_doc
    from jobs.tasks.embed import embed_sweep_task

    monkeypatch.setattr("jobs.tasks.embed.EMBED_SWEEP_CAP", 1)
    for n in (1, 2, 3, 4):
        await _seed_doc(pid, f"emb-sweep-rotate-{n}")

    with patch("embeddings._ensure_config", new_callable=AsyncMock):
        for _ in range(12):
            await embed_sweep_task({"redis": None})
    seen = {c.args[1] for c in enqueue_recorder.of("embed_document_task")}

    assert len(seen) > 1, (
        f"every run picked the same document ({seen}) — a stuck head starves the tail"
    )

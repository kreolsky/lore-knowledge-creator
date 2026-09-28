"""Boot-time backfill of doc_chunks.kind — the heal for rows predating the column.

`kind` is written at every chunk write from the parent's flags; for rows that
predate the column it is exactly derivable, so the web lifespan backfills them
once after apply_schema. `model` is NOT derivable (a matching content_hash only
proves the text, never which model produced the STORED vector) and must survive
the backfill untouched — a NONE model means "unknown, pre-column" and stays
until the document's next re-embed stamps it.
"""

import pytest_asyncio

from db import create_record, get_db

_CREATED: list[str] = []


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_seeded(test_db):
    """Remove every document/chunk row these tests seed (corpus-wide backfill
    calls elsewhere must not see them)."""
    yield
    db = await get_db()
    for doc_id in _CREATED:
        await db.query("DELETE type::record('documents', $id)", {"id": doc_id})
        await db.query("DELETE doc_chunks WHERE document_id = $id", {"id": doc_id})
    _CREATED.clear()


async def _seed_doc(doc_id: str, pid: str, *, content: str, host_id: str | None = None,
                    **extra) -> None:
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


async def _seed_chunk(doc_id: str, pid: str, *, suffix: str = "c0",
                      kind: str | None = None, model: str | None = None) -> None:
    """A minimal chunk row; kind/model are SET only when given (absent = NONE,
    the exact state of a pre-column row)."""
    db = await get_db()
    extra = ""
    params: dict = {"cid": f"{doc_id}-{suffix}", "did": doc_id, "pid": pid}
    if kind is not None:
        extra += ", kind = $kind"
        params["kind"] = kind
    if model is not None:
        extra += ", model = $model"
        params["model"] = model
    await db.query(
        "CREATE type::record('doc_chunks', $cid) SET document_id = $did, "
        "project_id = $pid, ord = 0, heading = NONE, content = 'text', "
        f"offset_start = 0, offset_end = 4, content_version = 0, embedding = [0.0]{extra}",
        params,
    )


def _kinds(rows: list[dict]) -> dict[str, str | None]:
    return {r["document_id"]: r["kind"] for r in rows}


async def _chunk_rows(ids: list[str]) -> list[dict]:
    return await (await get_db()).query(
        "SELECT document_id, kind, model FROM doc_chunks WHERE document_id IN $ids",
        {"ids": ids},
    )


async def test_none_kinds_filled_from_the_parent(project_with_doc):
    """One run fills every pre-column chunk with the parent's corpus label —
    the same rule the direct-hit layer applies: memory if is_memory, else
    reference, else document."""
    pid, host_id, _uid = project_with_doc
    from embedding_coverage import backfill_doc_chunk_kind

    await _seed_doc("bkf-doc", pid, content="plain document body")
    await _seed_doc("bkf-ref", pid, content="reference body",
                    host_id=host_id, is_reference=True)
    await _seed_doc("bkf-fact", pid, content="fact body",
                    is_memory=True, mem_active=True,
                    mem={"provenance": {"sources": []}})
    for did in ("bkf-doc", "bkf-ref", "bkf-fact"):
        await _seed_chunk(did, pid)

    updated = await backfill_doc_chunk_kind(await get_db())
    assert updated == 3
    kinds = _kinds(await _chunk_rows(["bkf-doc", "bkf-ref", "bkf-fact"]))
    assert kinds == {
        "bkf-doc": "document", "bkf-ref": "reference", "bkf-fact": "memory",
    }


async def test_second_run_is_a_noop_that_never_stoms(project_with_doc):
    """After the drain, a re-run writes nothing: the UPDATE's `AND kind IS NONE`
    guard means a chunk whose kind was later changed (by hand or by a future
    reclassification) is NEVER overwritten by the backfill."""
    pid, _host_id, _uid = project_with_doc
    from embedding_coverage import backfill_doc_chunk_kind

    await _seed_doc("bkf-once", pid, content="body one")
    await _seed_chunk("bkf-once", pid)
    assert await backfill_doc_chunk_kind(await get_db()) == 1

    db = await get_db()
    await db.query(
        "UPDATE type::record('doc_chunks', $cid) SET kind = 'reference'",
        {"cid": "bkf-once-c0"},
    )
    assert await backfill_doc_chunk_kind(db) == 0
    kinds = _kinds(await _chunk_rows(["bkf-once"]))
    assert kinds == {"bkf-once": "reference"}, "backfill re-stomped a set kind"


async def test_model_is_untouched(project_with_doc):
    """The backfill heals kind ONLY: a row with a hand-set model keeps it, a
    NONE-model row stays NONE (unknown provenance, not 'current model')."""
    pid, host_id, _uid = project_with_doc
    from embedding_coverage import backfill_doc_chunk_kind

    await _seed_doc("bkf-doc2", pid, content="plain body two")
    await _seed_doc("bkf-ref2", pid, content="ref body two",
                    host_id=host_id, is_reference=True)
    await _seed_chunk("bkf-doc2", pid, model="hand-set-model")
    await _seed_chunk("bkf-ref2", pid)

    await backfill_doc_chunk_kind(await get_db())

    rows = {r["document_id"]: r for r in await _chunk_rows(["bkf-doc2", "bkf-ref2"])}
    assert rows["bkf-doc2"]["model"] == "hand-set-model"
    assert rows["bkf-ref2"]["model"] is None
    assert rows["bkf-doc2"]["kind"] == "document"
    assert rows["bkf-ref2"]["kind"] == "reference"

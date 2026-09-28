"""Tests for atomic mention-edge rebuild (single-string BEGIN/COMMIT).

The rebuild MUST wrap DELETE + RELATEs in one BEGIN/COMMIT query string so a
DB-level failure rolls the whole batch back. The db.transaction() context
manager does NOT provide real rollback here (separate query() RPCs do not share
a transaction scope), so these tests guard the single-string contract directly.
"""

import pytest

from db import create_record, get_db
from mentions import rebuild_doc_mentions


async def _seed_doc(doc_id: str) -> None:
    try:
        await create_record("projects", "mentions-atomic-project", {
            "name": "Mentions Atomic", "status": "active",
        })
    except RuntimeError:
        pass
    await create_record("documents", doc_id, {
        "project_id": "mentions-atomic-project",
        "parent_id": None,
        "title": f"Doc {doc_id}",
        "path": f"/{doc_id}",
        "is_reference": False,
    })


async def _mention_targets(source_id: str) -> list[str]:
    db = await get_db()
    rows = await db.query(
        "SELECT VALUE meta::id(out) FROM doc_mentions "
        "WHERE in = type::record('documents', $id)",
        {"id": source_id},
    )
    return sorted(rows or [])


@pytest.mark.asyncio
async def test_rebuild_happy_path(test_db):
    await _seed_doc("atomic-src")
    await _seed_doc("atomic-old")
    await _seed_doc("atomic-new")

    db = await get_db()
    await rebuild_doc_mentions(db, "documents", "atomic-src", "[old](doc:atomic-old)")
    assert await _mention_targets("atomic-src") == ["atomic-old"]

    await rebuild_doc_mentions(db, "documents", "atomic-src", "[new](doc:atomic-new)")
    assert await _mention_targets("atomic-src") == ["atomic-new"]


async def _seed_ref(ref_id: str) -> None:
    try:
        await create_record("projects", "mentions-atomic-project", {
            "name": "Mentions Atomic", "status": "active",
        })
    except RuntimeError:
        pass
    # Reference-host invariant: attach to a real host doc (idempotent).
    host_id = "mentions-atomic-host"
    try:
        await create_record("documents", host_id, {
            "project_id": "mentions-atomic-project", "parent_id": None,
            "title": "Host", "path": "/host", "is_reference": False,
        })
    except RuntimeError:
        pass
    await create_record("documents", ref_id, {
        "project_id": "mentions-atomic-project",
        "parent_id": host_id,
        "title": f"Ref {ref_id}",
        "path": f"/{ref_id}",
        "is_reference": True,
    })


@pytest.mark.asyncio
async def test_rebuild_creates_edge_for_ref_mention(test_db):
    """`[text](ref:id)` links must create a doc_mentions edge so the referenced
    entity sees its incoming links. References live in the documents table, so the
    edge target is documents:ref_id."""
    await _seed_doc("atomic-ref-src")
    await _seed_ref("atomic-ref-target")

    db = await get_db()
    await rebuild_doc_mentions(
        db, "documents", "atomic-ref-src", "[trope](ref:atomic-ref-target)"
    )
    assert await _mention_targets("atomic-ref-src") == ["atomic-ref-target"]


@pytest.mark.asyncio
async def test_rebuild_mixes_doc_and_ref_mentions(test_db):
    await _seed_doc("atomic-mix-src")
    await _seed_doc("atomic-mix-doc")
    await _seed_ref("atomic-mix-ref")

    db = await get_db()
    await rebuild_doc_mentions(
        db, "documents", "atomic-mix-src",
        "[d](doc:atomic-mix-doc) [r](ref:atomic-mix-ref)",
    )
    assert await _mention_targets("atomic-mix-src") == ["atomic-mix-doc", "atomic-mix-ref"]


@pytest.mark.asyncio
async def test_rebuild_uses_single_begin_commit_query(test_db, monkeypatch):
    """Contract guard: the rebuild issues exactly one BEGIN/COMMIT-wrapped query,
    not per-statement auto-committing calls (which caused the backlink-wipe bug)."""
    await _seed_doc("atomic-src2")
    await _seed_doc("atomic-t1")
    await _seed_doc("atomic-t2")

    db = await get_db()
    captured: list[str] = []
    orig_query = db.query

    async def spy_query(q, *args, **kwargs):
        captured.append(q)
        return await orig_query(q, *args, **kwargs)

    monkeypatch.setattr(db, "query", spy_query)
    await rebuild_doc_mentions(
        db, "documents", "atomic-src2",
        "[t1](doc:atomic-t1) [t2](doc:atomic-t2)",
    )
    monkeypatch.undo()

    rebuild_queries = [q for q in captured if "doc_mentions" in q]
    assert len(rebuild_queries) == 1, "rebuild must be a single query, got multiple"
    q = rebuild_queries[0]
    assert q.strip().upper().startswith("BEGIN TRANSACTION")
    assert q.strip().upper().endswith("COMMIT TRANSACTION")
    assert "DELETE doc_mentions" in q
    assert q.count("RELATE") == 2


@pytest.mark.asyncio
async def test_db_rolls_back_delete_on_mid_transaction_error(test_db):
    """Direct DB proof: a failing statement inside a single-string BEGIN/COMMIT
    rolls back the preceding DELETE — the property the rebuild relies on."""
    db = await get_db()
    # DEFINE first — surrealdb 2.0.0 raises NotFoundError on DELETE/SELECT against a table
    # that does not exist yet (1.0.4 returned None). On a fresh DB the table is absent.
    await db.query("DEFINE TABLE IF NOT EXISTS probe_atomic SCHEMALESS")
    await db.query("DELETE probe_atomic")
    await db.query("CREATE probe_atomic:keep SET n = 1")

    await db.query(
        "BEGIN TRANSACTION; "
        "DELETE probe_atomic WHERE id = probe_atomic:keep; "
        'THROW "simulated failure"; '
        "COMMIT TRANSACTION"
    )
    rows = await db.query("SELECT VALUE id FROM probe_atomic")
    assert len(rows or []) == 1, "DELETE must be rolled back by the failed transaction"

    await db.query("DELETE probe_atomic")

"""Full table-state in checkpoints — unified writer + restore (plan: snapshot-tables-full-state).

Covers the DB-integration half of the feature (the pure capture/apply logic lives in
test_table_capture.py):
  - the unified writer persists the full field set for every creation path;
  - restore rebuilds the `tables` Yjs subtree from `tables_json`;
  - integrity validates `tables_hash` (incl. the mixed-null partial-migration edge);
  - legacy checkpoints (no tables_json) restore via the fallback without error.
"""
import json

import pytest
from cp_store import create_checkpoint

TABLES_PAYLOAD = json.dumps({
    "t1": {"columns": [200, 320], "rows": [["a", "b"], ["c", "d"]]},
})


# ── unified writer ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_writer_persists_full_field_set(test_db):
    await test_db.query("DELETE checkpoints")
    cp = await create_checkpoint(
        document_id="doc-1",
        content="![t](table:t1)",
        tables_json=TABLES_PAYLOAD,
        label="manual",
        comment=None,
        created_by="u1",
    )
    row = await test_db.query(
        "SELECT content_ref, content_hash, tables_json, tables_hash FROM checkpoints "
        "WHERE document_id = 'doc-1'",
    )
    row = row[0]
    assert row["content_ref"]
    assert row["content_hash"]
    assert row["tables_json"] == TABLES_PAYLOAD
    assert row["tables_hash"]
    # writer returns the serialized row (no user_name attached).
    assert cp["document_id"] == "doc-1"


@pytest.mark.asyncio
async def test_writer_none_tables_json_leaves_fields_null(test_db):
    await test_db.query("DELETE checkpoints")
    await create_checkpoint(
        document_id="doc-2", content="plain", tables_json=None,
        label="manual", comment=None, created_by="u1",
    )
    row = (await test_db.query(
        "SELECT tables_json, tables_hash FROM checkpoints WHERE document_id = 'doc-2'",
    ))[0]
    assert row["tables_json"] is None
    assert row["tables_hash"] is None


@pytest.mark.asyncio
async def test_writer_deterministic_id_upserts_last_session(test_db):
    """The uuid5 id path (last-session) must upsert one stable row, not CREATE a new one.

    Post C3-A the row stores content only in content_ref (inline content is None);
    the upsert invariant (one stable row, newest content wins) is asserted via the blob.
    """
    from uuid import NAMESPACE_URL, uuid5

    from cp_store import get_content

    await test_db.query("DELETE checkpoints")
    stable_id = str(uuid5(NAMESPACE_URL, "last-session:doc-3:u9"))
    await create_checkpoint(
        document_id="doc-3", content="first", tables_json="{}",
        label="last-session", comment=None, created_by="u9", id=stable_id,
    )
    await create_checkpoint(
        document_id="doc-3", content="second", tables_json="{}",
        label="last-session", comment=None, created_by="u9", id=stable_id,
    )
    rows = await test_db.query("SELECT content_ref FROM checkpoints WHERE document_id = 'doc-3'")
    assert len(rows) == 1
    assert await get_content(rows[0]["content_ref"]) == "second"


# ── serialization (list excludes, get includes) ────────────────────────────────


@pytest.mark.asyncio
async def test_integrity_tables_json_tamper_drives_invalid(test_db):
    # Was a duplicate-named test (shadowed the mixed-null case below) referencing an
    # undefined validate_checkpoint_integrity — renamed + import added so it actually runs.
    from auto_backup import validate_checkpoint_integrity

    await test_db.query("DELETE checkpoints")
    cp = await create_checkpoint(
        document_id="doc-i", content="hello", tables_json=TABLES_PAYLOAD,
        label="manual", comment=None, created_by="u1",
    )
    cp_id = cp["checkpoint_id"]

    ok = await validate_checkpoint_integrity(cp_id)
    assert ok["valid"] is True

    # Tamper with tables_json → tables_valid False → combined valid False.
    await test_db.query(
        "UPDATE type::record('checkpoints', $id) SET tables_json = 'tampered'",
        {"id": cp_id},
    )
    bad = await validate_checkpoint_integrity(cp_id)
    assert bad["valid"] is False


@pytest.mark.asyncio
async def test_integrity_mixed_null_tables_hash_does_not_short_circuit(test_db):
    """content_hash present, tables_hash null (partial migration) → valid per content,
    tables treated as legacy-pass — combined result reflects BOTH fields."""
    from auto_backup import validate_checkpoint_integrity

    await test_db.query("DELETE checkpoints")
    content = "mixed null body"
    cp = await create_checkpoint(
        document_id="doc-m", content=content, tables_json=None,
        label="manual", comment=None, created_by="u1",
    )
    cp_id = cp["checkpoint_id"]
    ok = await validate_checkpoint_integrity(cp_id)
    # tables_hash is null → tables is legacy-pass; content matches → valid True.
    assert ok["valid"] is True

    # Now corrupt the CONTENT hash only; tables still null. Must NOT short-circuit on the
    # null tables field and return True — a content-hash mismatch drives valid False.
    # (The blob is immutable; corrupt the stored content_hash to force the mismatch.)
    await test_db.query(
        "UPDATE type::record('checkpoints', $id) SET content_hash = 'deadbeef'",
        {"id": cp_id},
    )
    bad = await validate_checkpoint_integrity(cp_id)
    assert bad["valid"] is False


# ─── C3-A: new checkpoints do NOT double-write inline content ────────────────


@pytest.mark.asyncio
async def test_writer_does_not_write_inline_content(test_db):
    """create_checkpoint must store content only in the blob (content_ref), not
    inline. Why: double-writing unbounds checkpoints growth at ~2× the blob
    payload and defeats the null-out migration (the writer keeps re-feeding it).
    The column stays for legacy rows; readers resolve via content_ref first.
    """
    await test_db.query("DELETE checkpoints")
    await create_checkpoint(
        document_id="doc-c3a", content="inline-should-be-gone",
        tables_json=None, label="manual", comment=None, created_by="u1",
    )
    row = (await test_db.query(
        "SELECT content, content_ref, content_hash FROM checkpoints "
        "WHERE document_id = 'doc-c3a'",
    ))[0]
    assert row["content"] is None, "new checkpoint must not store inline content"
    assert row["content_ref"]
    assert row["content_hash"]


@pytest.mark.asyncio
async def test_writer_deterministic_id_upsert_keeps_content_null(test_db):
    """The uuid5 id UPSERT path (last-session) must also stop writing inline content."""
    from uuid import NAMESPACE_URL, uuid5

    await test_db.query("DELETE checkpoints")
    stable_id = str(uuid5(NAMESPACE_URL, "last-session:doc-c3a-up:u9"))
    await create_checkpoint(
        document_id="doc-c3a-up", content="first", tables_json="{}",
        label="last-session", comment=None, created_by="u9", id=stable_id,
    )
    row = (await test_db.query(
        "SELECT content FROM checkpoints WHERE document_id = 'doc-c3a-up'",
    ))[0]
    assert row["content"] is None

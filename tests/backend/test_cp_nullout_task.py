"""C3-B: arq nullout task — irreversible inline-content null-out, guard-gated.

The null-out (DEFINE FIELD OVERWRITE + UPDATE content=NONE) is irreversible, so
it stays OUT of the startup migration runner. It runs as an arq cron job gated
by the SAME guards as the CLI migration: abort (no-op + log) unless every row has
a content_ref AND every blob resolves with a matching hash. Idempotent.
"""
import pytest


async def _row_content(test_db, cp_id):
    rows = await test_db.query(
        "SELECT content, content_ref FROM type::record('checkpoints', $id)",
        {"id": cp_id},
    )
    return rows[0] if rows else None


@pytest.mark.asyncio
async def test_nullout_task_aborts_when_backfill_incomplete(test_db):
    """A row with inline content but no content_ref must NOT be nulled — guard holds."""
    from jobs.tasks import nullout_inline_content_task

    await test_db.query("DELETE checkpoints")
    await test_db.query(
        "CREATE type::record('checkpoints', 'cp-unmig') CONTENT {"
        " document_id: 'd1', content: 'legacy inline', content_ref: NONE,"
        " content_hash: NONE, label: 'manual' }",
    )
    # Guard fails → task must no-op (not raise, so arq doesn't retry-storm).
    await nullout_inline_content_task({})
    row = await _row_content(test_db, "cp-unmig")
    assert row["content"] == "legacy inline", "guard must hold: no null-out before backfill"


@pytest.mark.asyncio
async def test_nullout_task_nulls_after_backfill_and_is_idempotent(test_db):
    """With every row backfilled (content_ref set + blob resolves), the task nulls
    inline content. A second run is a no-op (idempotent). GET still resolves via blob."""
    from cp_store import get_content, hash_content, put_content

    from jobs.tasks import nullout_inline_content_task

    await test_db.query("DELETE cp_blobs")
    await test_db.query("DELETE checkpoints")
    content = "fully migrated body"
    ref = await put_content(content)
    assert hash_content(content) == ref
    await test_db.query(
        "CREATE type::record('checkpoints', 'cp-mig') CONTENT {"
        " document_id: 'd2', content: $c, content_ref: $ref, content_hash: $ref,"
        " label: 'manual' }",
        {"c": content, "ref": ref},
    )

    await nullout_inline_content_task({})
    row = await _row_content(test_db, "cp-mig")
    assert row["content"] is None, "inline content must be nulled after backfill"
    assert row["content_ref"] == ref
    # Blob still resolves → restorability preserved.
    assert await get_content(ref) == content

    # Idempotent re-run: no rows with inline content remain → no-op, no error.
    await nullout_inline_content_task({})
    row = await _row_content(test_db, "cp-mig")
    assert row["content"] is None

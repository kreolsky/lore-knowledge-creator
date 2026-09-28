"""Atomic compaction regression — concurrent append must not be silently lost.

See plan: ~/.kilo/plans/1782941631770-snapshot-subsystem-fixes.md (FIX C1).

maybe_compact merges ydoc_updates into ydoc_state then prunes the log. A
concurrent append landing between snapshot-capture and prune used to vanish
(the prune was an unbounded `DELETE WHERE document_id = $id`). The fix prunes
only the exact replayed row ids inside a transaction.
"""

import pytest
from collab.registry import _get_or_create_session, _session_key, _sessions
from helpers import apply_binary_edit
from pycrdt import Doc, Text

from db import get_db


async def _ydoc_updates_count(doc_id: str) -> int:
    db = await get_db()
    rows = await db.query(
        "SELECT count() AS total FROM ydoc_updates WHERE document_id = $id GROUP ALL",
        {"id": doc_id},
    )
    return rows[0].get("total", 0) if rows else 0


async def _seed_log_rows(doc_id: str, n: int) -> None:
    """Append n distinct update rows to the ydoc log via a live session.

    Each edit appends a row immediately (publish_doc_update append=True). We do
    NOT call flush_to_db here: since §3.3 flush runs maybe_compact, a flush with
    ≥ COMPACT_MIN_UPDATES rows would compact the very rows we are seeding away.
    Seeding appends raw log rows and leaves ydoc_state to be reconstructed by
    load()/maybe_compact replaying the log.
    """
    session = await _get_or_create_session("doc", doc_id, "")
    for i in range(n):
        await apply_binary_edit(session, f"r{i}", at=0)
    _sessions.pop(_session_key("doc", doc_id), None)
    assert await _ydoc_updates_count(doc_id) >= n


@pytest.mark.asyncio
async def test_compact_does_not_lose_concurrent_append(collab_project, _clear_sessions, monkeypatch):
    """An append landing between compaction's snapshot-capture and its prune survives.

    Reproduces the silent-data-loss race: load() captures the snapshot from
    rows r1..r10, then a concurrent append adds r11, then the prune runs. Under
    the old `DELETE WHERE document_id = $id` prune, r11 (never in the snapshot)
    is wiped. Under id-set pruning, r11's id is not in the replayed set, so it
    survives in the log and replays on the next load().
    """
    import ydoc_store
    from ydoc_store import append_update, load, maybe_compact

    _, doc_id, *_ = collab_project
    from ydoc_store import COMPACT_MIN_UPDATES

    await _seed_log_rows(doc_id, COMPACT_MIN_UPDATES)

    # Build the update bytes for the "concurrent" edit against the live doc so
    # applying it later is a valid CRDT op.
    base = await load(doc_id)
    replica = Doc()
    replica.apply_update(base.get_update())
    replica.get("content", type=Text).insert(0, "CONCURRENT")
    concurrent_update = replica.get_update(base.get_state())

    injected = {"done": False}

    async def _injecting_load(entity_id):
        # maybe_compact calls load() AFTER capturing the replayed row ids. We
        # run the real load (snapshot source = rows at this moment), then inject
        # a concurrent append BEFORE the prune transaction runs.
        doc = await _real_load(entity_id)
        if not injected["done"]:
            await append_update(entity_id, concurrent_update)
            injected["done"] = True
        return doc

    _real_load = ydoc_store.load
    monkeypatch.setattr(ydoc_store, "load", _injecting_load)

    await maybe_compact(doc_id)

    assert injected["done"], "test harness did not inject the concurrent append"
    # The concurrent row must still be in the log (its id was never replayed).
    assert await _ydoc_updates_count(doc_id) >= 1

    # And re-loading must surface the concurrent edit — it was not silently lost.
    reloaded = await load(doc_id)
    assert "CONCURRENT" in str(reloaded.get("content", type=Text))


@pytest.mark.asyncio
async def test_compact_id_set_not_created_at_cutoff(collab_project, _clear_sessions, monkeypatch):
    """Pin the id-vs-timestamp decision.

    The prune deletes by the replayed ROW IDS, not by a `created_at <= cutoff`
    predicate. Why: under high write concurrency a concurrent append can get the
    SAME created_at as the last replayed row; a `<=` cutoff would wrongly delete
    it even though it was never in the snapshot. Deleting by id is correct
    regardless of timestamp resolution.
    """
    import ydoc_store
    from ydoc_store import COMPACT_MIN_UPDATES, load, maybe_compact

    _, doc_id, *_ = collab_project
    await _seed_log_rows(doc_id, COMPACT_MIN_UPDATES)

    db = await get_db()
    # Find the last replayed row's created_at so we can force a collision.
    rows = await db.query(
        "SELECT meta::id(id) AS rid, created_at FROM ydoc_updates "
        "WHERE document_id = $id ORDER BY created_at ASC",
        {"id": doc_id},
    )
    last_created_at = rows[-1]["created_at"]

    base = await load(doc_id)
    replica = Doc()
    replica.apply_update(base.get_update())
    replica.get("content", type=Text).insert(0, "SAME_TS")
    same_ts_update = replica.get_update(base.get_state())

    async def _injecting_load(entity_id):
        doc = await _real_load(entity_id)
        if not _injected["done"]:
            # Insert with the SAME created_at as the last replayed row to force
            # the collision a naive `created_at <=` cutoff would mishandle.
            await db.query(
                "CREATE ydoc_updates CONTENT { document_id: $id, payload: $upd, created_at: $ts }",
                {"id": entity_id, "upd": same_ts_update, "ts": last_created_at},
            )
            _injected["done"] = True
        return doc

    _real_load = ydoc_store.load
    _injected = {"done": False}
    monkeypatch.setattr(ydoc_store, "load", _injecting_load)

    await maybe_compact(doc_id)
    assert _injected["done"]

    reloaded = await load(doc_id)
    assert "SAME_TS" in str(reloaded.get("content", type=Text))

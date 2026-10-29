"""D5 — a run must report its own shape: facts-per-reference + batches-per-reference.

The extraction quota surfaced only because someone read the database by hand: gemini/pro
emitted exactly 5 verdicts per reference, one apply call each, and nothing in the run's
own output named that shape. facts-per-reference is recoverable from provenance (the one
thread from a fact back to its source material); batches-per-reference is not, so it is
counted per run, ephemerally, and both are surfaced in the apply result next to `stats`
and in the closing portion. Sourced from what the server did, not what the agent recalls.
"""


import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def _clean_memory_space(project_with_doc, test_db):
    """Hermetic: the loop test drives `_next_portion`, which would otherwise see stale
    unstamped references left by other tests. Wipe references + memory facts + chunks."""
    pid = project_with_doc[0]
    await test_db.query(
        "DELETE documents WHERE project_id = $pid AND is_memory = true", {"pid": pid},
    )
    await test_db.query("DELETE doc_chunks WHERE project_id = $pid", {"pid": pid})
    await test_db.query(
        "DELETE documents WHERE project_id = $pid AND is_reference = true", {"pid": pid},
    )


async def test_batches_counter_degrades_to_none_when_the_store_is_down(monkeypatch):
    """Unit pin for the counter's OWN fail-open: incr_reference_batches catches a
    store outage and returns None — the report degrades, nothing raises. Reachable
    when Redis drops AFTER the lock was acquired (mid-apply); the apply-level
    from-the-start outage is pinned by the fails-closed test above."""
    import redis_pool
    from memory.run_key import incr_reference_batches

    async def _boom_get_redis(*_a, **_k):
        raise RuntimeError("store down")

    monkeypatch.setattr(redis_pool, "get_redis", _boom_get_redis)
    assert await incr_reference_batches("run-1", "ref-1") is None

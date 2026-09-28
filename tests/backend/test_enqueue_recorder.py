"""Tests for the shared enqueue double — `enqueue_recorder.EnqueueRecorder`.

Every production consumer calls the queue qualified (`jobs_pool.enqueue`), so
the double patches ONE name — `jobs.pool.enqueue` — and there are no
module-scope `enqueue` holders left to derive or repair. These tests pin that
contract, including the capture case from the plan: a qualified call made from
a real consumer module (`files_service`) must land in `.calls`, so a recorder
patching a name nobody reads cannot pass as a vacuous green.
"""

import files_service
import files_util
from enqueue_recorder import EnqueueRecorder

import jobs.pool as jobs_pool


async def test_fake_records_name_args_kwargs_and_returns_a_landed_job():
    """Tuple-equality contract the converted tuple sites depend on, plus the
    return the double owes the real symbol.

    The fake must mirror `jobs.pool.enqueue`: a Job stand-in for a LANDED
    posting, None only for a dedup collapse. Returning None unconditionally
    made every recorded posting read as collapsed to a caller that counts what
    landed (the embed sweep's per-run cap), so the double would report a cap
    spent on nothing while the real symbol reported it spent on work.
    """
    with EnqueueRecorder.active() as rec:
        result = await jobs_pool.enqueue("t", "a", k=1)

    assert result is not None, "a recorded posting is a LANDED one, not a collapse"
    assert rec.calls == [("t", ("a",), {"k": 1})]


async def test_direct_jobs_pool_enqueue_routes_to_recorder_while_active():
    live = jobs_pool.enqueue
    with EnqueueRecorder.active() as rec:
        assert jobs_pool.enqueue is not live
        await jobs_pool.enqueue("direct")
    assert jobs_pool.enqueue is live
    assert rec.names() == ["direct"]


async def test_qualified_consumer_call_is_captured():
    """The plan's capture case: `files_service` calls the queue through its
    `jobs_pool` module attribute — the recorder must capture that call form,
    and the consumer must carry no frozen `enqueue` binding the patch would
    miss. Why this case exists: if the recorder patched a name consumers no
    longer read, every other test here would still pass — the miss would be
    silent."""
    assert not hasattr(files_service, "enqueue"), (
        "files_service holds a module-scope `enqueue` binding — a frozen "
        "from-import the single patch site cannot reach"
    )
    with EnqueueRecorder.active() as rec:
        await files_service.jobs_pool.enqueue("convert_docx_task", "ref-1", "u")
    assert rec.of("convert_docx_task") == [
        ("convert_docx_task", ("ref-1", "u"), {})
    ]


async def test_second_activation_sees_clean_binding():
    live = jobs_pool.enqueue
    with EnqueueRecorder.active() as first:
        await jobs_pool.enqueue("one")
    with EnqueueRecorder.active() as second:
        assert jobs_pool.enqueue is not live
        await jobs_pool.enqueue("two")
    assert jobs_pool.enqueue is live
    assert files_util.jobs_pool.enqueue is live
    assert first.names() == ["one"]
    assert second.names() == ["two"]


async def test_dead_fake_reinstalled_by_external_undo_is_recovered():
    """LIFO-teardown break: a patcher started INSIDE the window (e.g.
    `monkeypatch.setattr("jobs.pool.enqueue", ...)`) saves our fake as its
    "original"; when its undo fires AFTER the recorder exited, it re-installs
    the DEAD fake over jobs.pool.enqueue. The next activation must recover the
    live symbol first — otherwise the window silently records nothing while
    the real queue runs."""
    live = jobs_pool.enqueue
    with EnqueueRecorder.active():
        externally_saved = jobs_pool.enqueue  # what that patcher saved as "original"
    jobs_pool.enqueue = externally_saved  # its undo, after the recorder exited
    assert jobs_pool.enqueue is not live
    try:
        with EnqueueRecorder.active() as second:
            assert jobs_pool.enqueue is not externally_saved
            await jobs_pool.enqueue("recovered")
        assert second.names() == ["recovered"]
    finally:
        jobs_pool.enqueue = live
    assert jobs_pool.enqueue is live


async def test_nested_activation_keeps_both_windows_capturing():
    """Dead-fake recovery must NOT fire on an OUTER window's fake (it is
    alive, not dead): nested activations stay independent and the outer
    recording survives the inner exit."""
    with EnqueueRecorder.active() as outer:
        await jobs_pool.enqueue("outer-early")
        with EnqueueRecorder.active() as inner:
            await jobs_pool.enqueue("inner")
        await jobs_pool.enqueue("outer-late")
    assert outer.names() == ["outer-early", "outer-late"]
    assert inner.names() == ["inner"]
    assert jobs_pool.enqueue is not None

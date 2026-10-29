"""Tests for arq job queue — enqueue, dedup, defer.

The suite runs against Redis DB 15 (set in conftest); the live worker containers
consume DB 0, so jobs enqueued here stay queued/deferred for inspection and are
flushed between tests by the _redis_isolation fixture.
"""


class TestJobQueue:


    async def test_enqueue_returns_the_job_and_none_on_dedup_collapse(self):
        """jobs.pool.enqueue hands back arq's return value verbatim.

        Why: None is arq's signal that the stable job_id collapsed onto an
        already-pending job — the only way a caller can tell a posting that
        will run from one the debounce dedup swallowed. The hourly embed
        sweep's per-run cap counts landed postings with it.
        """
        from unittest.mock import AsyncMock, patch

        import jobs.pool as jobs_pool

        job = object()
        pool = AsyncMock()
        pool.enqueue_job = AsyncMock(return_value=job)
        with patch("jobs.pool.get_arq_pool", return_value=pool):
            assert await jobs_pool.enqueue("embed_document_task", "doc", "d1", "p1") is job
            pool.enqueue_job.return_value = None
            assert await jobs_pool.enqueue("embed_document_task", "doc", "d1", "p1") is None

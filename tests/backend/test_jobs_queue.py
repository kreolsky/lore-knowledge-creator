"""Tests for arq job queue — enqueue, dedup, defer.

The suite runs against Redis DB 15 (set in conftest); the live worker containers
consume DB 0, so jobs enqueued here stay queued/deferred for inspection and are
flushed between tests by the _redis_isolation fixture.
"""

import pytest
import pytest_asyncio
from arq.jobs import Job, JobStatus


@pytest_asyncio.fixture
async def arq_pool():
    from jobs.pool import close_arq_pool, get_arq_pool
    pool = await get_arq_pool()
    yield pool
    await close_arq_pool()


class TestJobQueue:
    @pytest.mark.asyncio
    async def test_enqueue_wrapper_queues_job(self, arq_pool):
        """The enqueue() wrapper passes job_id through and queues the job."""
        from jobs import pool as jobs_pool
        job_id = "test-enqueue-001"

        await jobs_pool.enqueue("thumbnail_task", "proj1", "ref1", "path1", job_id=job_id)

        assert await Job(job_id, redis=arq_pool).status() == JobStatus.queued

    @pytest.mark.asyncio
    async def test_dedup_same_job_id(self, arq_pool):
        """Enqueuing a job_id already queued is a no-op (built-in dedup)."""
        job_id = "test-dedup-001"

        first = await arq_pool.enqueue_job("thumbnail_task", "proj1", "ref1", "path1", _job_id=job_id)
        second = await arq_pool.enqueue_job("thumbnail_task", "proj1", "ref1", "path1", _job_id=job_id)

        assert first is not None
        assert second is None  # already queued → dedup
        assert await Job(job_id, redis=arq_pool).status() == JobStatus.queued

    def test_transcription_worker_max_tries(self):
        """TranscriptionWorkerSettings registers transcribe_task with max_tries=2.

        Plan §Retry/dead-letter: transcription gets max_tries=2 so the dead-letter
        branch (status='error' + transcription_error event) fires on the 2nd attempt,
        not arq's default 5.
        """
        from jobs.worker import TranscriptionWorkerSettings

        fns = {getattr(f, "name", None): f for f in TranscriptionWorkerSettings.functions}
        assert "transcribe_task" in fns
        assert fns["transcribe_task"].max_tries == 2

    def test_embed_registration_shares_task_budget_constant(self):
        """WorkerSettings registers embed_document_task with EMBED_MAX_TRIES — ONE
        constant shared with the task, never two literals.

        Why: the task compares ctx["job_try"] against the same value to dead-letter
        on its FINAL attempt (arq aborts with `job_try > max_tries` WITHOUT invoking
        the task, so no recorder would run on that path). A registration literal that
        drifted from the task's constant would make the task raise Retry on its last
        attempt straight into arq's silent terminal path — the 187-doc prod hole of
        2026-08-18 (docs with embedding_status='ok' and zero chunks).
        """
        from jobs.tasks.embed import EMBED_MAX_TRIES
        from jobs.worker import WorkerSettings

        fns = {getattr(f, "name", None): f for f in WorkerSettings.functions}
        assert "embed_document_task" in fns
        assert fns["embed_document_task"].max_tries == EMBED_MAX_TRIES

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

    def test_transcription_worker_consumes_transcription_queue(self):
        """TranscriptionWorkerSettings.queue_name must match the queue used at the
        enqueue sites (queue='transcription'), else jobs are never consumed.

        Regression: without queue_name the worker polls the default 'arq:queue' while
        transcribe jobs land in 'transcription' → transcription silently never runs.
        """
        from jobs.worker import TranscriptionWorkerSettings

        assert TranscriptionWorkerSettings.queue_name == "transcription"

    def test_workers_do_not_retain_results(self):
        """Both worker classes set keep_result=0.

        Regression: arq's enqueue_job dedups on arq:result:{id} too, so any retention
        blocks reuse of a stable job_id (embed:/transcribe:/backup:…) for that window —
        breaking re-trigger/debounce. Failures are recorded in the DB, not arq results.

        NOTE: the setting MUST be named `keep_result` — arq's get_kwargs only forwards
        attributes matching the Worker signature, so a misnamed `keep_result_s` is
        silently dropped and arq falls back to its 3600s default (this was the bug).
        """
        import inspect

        from arq.worker import Worker

        from jobs.worker import TranscriptionWorkerSettings, WorkerSettings

        assert "keep_result" in inspect.signature(Worker).parameters
        assert WorkerSettings.keep_result == 0
        assert TranscriptionWorkerSettings.keep_result == 0

    @pytest.mark.asyncio
    async def test_job_id_reusable_after_completion(self, arq_pool):
        """With keep_result_s=0 a stable job_id is enqueue-able again once the prior
        run is terminal — no lingering arq:result:{id} blocking reuse."""
        from arq.constants import result_key_prefix

        job_id = "test-reuse-001"
        first = await arq_pool.enqueue_job("thumbnail_task", "p", "r", "x", _job_id=job_id)
        assert first is not None

        # Simulate a completed run that retained NO result (keep_result_s=0):
        # drop the job key + ensure no result key, then re-enqueue must be accepted.
        await arq_pool.delete(f"arq:job:{job_id}")
        assert await arq_pool.exists(result_key_prefix + job_id) == 0

        second = await arq_pool.enqueue_job("thumbnail_task", "p", "r", "x", _job_id=job_id)
        assert second is not None  # reuse allowed

    @pytest.mark.asyncio
    async def test_defer_schedules_later(self, arq_pool):
        """defer schedules the job in the future (deferred status, not queued)."""
        from jobs import pool as jobs_pool
        job_id = "test-defer-001"

        await jobs_pool.enqueue("thumbnail_task", "proj1", "ref1", "path1", job_id=job_id, defer=30)

        assert await Job(job_id, redis=arq_pool).status() == JobStatus.deferred

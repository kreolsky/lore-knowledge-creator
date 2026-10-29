"""Tests for the arq embed_document_task and the _on_content_flushed trigger.

Phase E migration: embeddings debounce no longer uses in-process _pending_tasks +
asyncio.sleep+cancel. _on_content_flushed sets a Redis deadline key and enqueues
embed_document_task via arq. When the deferred task runs, it re-defers itself via
arq Retry if a newer edit pushed the deadline forward (trailing debounce); otherwise
it embeds.
"""

import time
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_on_content_flushed_failed_status_skips_cooldown(enqueue_recorder):
    """When embedding_status='failed', enqueue without defer (immediate retry)."""
    from embeddings import _on_content_flushed

    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=[{"embedding_status": "failed"}])

    with patch("embeddings.get_db", return_value=mock_db):
        await _on_content_flushed("doc", "ent-fail", "proj-1")

    calls = enqueue_recorder.calls
    assert len(calls) == 1
    name, args, kwargs = calls[0]
    assert name == "embed_document_task"
    assert kwargs.get("defer") is None, "Should not defer when status is failed"


@pytest.mark.asyncio
async def test_embed_document_task_redefers_when_deadline_in_future():
    """Deadline pushed forward by a newer edit → task raises Retry, does NOT embed,
    and resets arq's retry counter so debounce re-defers don't count toward max_tries."""
    from arq.constants import retry_key_prefix
    from arq.worker import Retry

    from jobs.tasks import embed_document_task

    deleted = []
    deadline_time = time.time() + 30  # far in the future

    class FakeRedis:
        async def get(self, key):
            return str(deadline_time).encode()
        async def delete(self, key):
            deleted.append(key)

    ctx = {"redis": FakeRedis(), "job_id": "embed:ent-debounce", "job_try": 1, "max_tries": 4}

    with patch("embeddings._reembed", new_callable=AsyncMock) as mock_reembed, \
         patch("embeddings._on_embed_success", new_callable=AsyncMock):
        with pytest.raises(Retry):
            await embed_document_task(ctx, "doc", "ent-debounce", "proj-1")

    mock_reembed.assert_not_awaited()
    # retry counter reset so unbounded debounce re-defers don't exhaust max_tries
    assert retry_key_prefix + "embed:ent-debounce" in deleted


def _http_status_error(status: int):
    """A realistic httpx.HTTPStatusError for the embedding provider."""
    import httpx

    request = httpx.Request("POST", "http://embedding.test/v1/embeddings")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"Server error {status}", request=request, response=response)


@pytest.mark.asyncio
async def test_embed_document_task_transient_error_retries_below_budget():
    """A transient 503 on a NON-final try raises Retry and records NOTHING.

    Guards the recovery half of the budget split: only the final attempt may
    dead-letter. Dead-lettering early would terminate a doc whose next attempt
    might succeed — the transient branch exists so a provider blip does not
    drop the document out of the index.
    """
    from arq.worker import Retry

    from jobs.tasks import embed_document_task
    from jobs.tasks.embed import EMBED_MAX_TRIES

    class FakeRedis:
        async def get(self, key):
            return None
        async def delete(self, key):
            pass

    ctx = {"redis": FakeRedis(), "job_id": "embed:ent-503",
           "job_try": EMBED_MAX_TRIES - 1}

    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=None)

    # doc_outline.refresh_outline is mocked like the embeddings collaborators:
    # it is the task's OTHER pre-embed actor (plan document-outline-in-structure-
    # map) and would otherwise run its real DB read/write against the patched
    # db.get_db. This test isolates the retry-budget contract; the outline
    # ordering has its own tests in test_doc_outline.py.
    with patch("embeddings._reembed", side_effect=_http_status_error(503)), \
         patch("embeddings._on_embed_failure", new_callable=AsyncMock) as mock_failure, \
         patch("embeddings._on_embed_success", new_callable=AsyncMock), \
         patch("doc_outline.refresh_outline", new_callable=AsyncMock), \
         patch("db.get_db", return_value=mock_db):
        with pytest.raises(Retry):
            await embed_document_task(ctx, "doc", "ent-503", "proj-1")

    mock_failure.assert_not_awaited()
    mock_db.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_embed_document_task_connect_error_retries_on_first_try():
    """A dropped connection (TransportError) is transient like a 5xx — Retry on try 1.

    ConnectError carries no response, so before the TransportError branch it fell
    through to the generic dead-letter: one refused TCP connection terminated the
    document's embedding on the FIRST try instead of waiting out the outage.
    """
    import httpx
    from arq.worker import Retry

    from jobs.tasks import embed_document_task

    class FakeRedis:
        async def get(self, key):
            return None
        async def delete(self, key):
            pass

    ctx = {"redis": FakeRedis(), "job_id": "embed:ent-conn1", "job_try": 1}

    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=None)

    with patch("embeddings._reembed", side_effect=httpx.ConnectError("connection refused")), \
         patch("embeddings._on_embed_failure", new_callable=AsyncMock) as mock_failure, \
         patch("embeddings._on_embed_success", new_callable=AsyncMock), \
         patch("doc_outline.refresh_outline", new_callable=AsyncMock), \
         patch("db.get_db", return_value=mock_db):
        with pytest.raises(Retry):
            await embed_document_task(ctx, "doc", "ent-conn1", "proj-1")

    mock_failure.assert_not_awaited()
    mock_db.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_embed_document_task_connect_error_dead_letters_on_last_try():
    """ConnectError at the budget's end dead-letters naming the exception class.

    The reason string carries the CLASS NAME where an HTTP status would go — a
    dropped connection has no status, and a bare message would not tell an
    operator the failure class. Same EMBED_MAX_TRIES budget as a 5xx: transient
    means retried, not retried forever.
    """
    import httpx

    from jobs.tasks import embed_document_task
    from jobs.tasks.embed import EMBED_MAX_TRIES

    class FakeRedis:
        async def get(self, key):
            return None
        async def delete(self, key):
            pass

    ctx = {"redis": FakeRedis(), "job_id": "embed:ent-conn-last",
           "job_try": EMBED_MAX_TRIES}

    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=None)

    with patch("embeddings._reembed", side_effect=httpx.ConnectError("connection refused")), \
         patch("embeddings._on_embed_failure", new_callable=AsyncMock) as mock_failure, \
         patch("embeddings._on_embed_success", new_callable=AsyncMock), \
         patch("doc_outline.refresh_outline", new_callable=AsyncMock), \
         patch("db.get_db", return_value=mock_db):
        with pytest.raises(httpx.ConnectError):
            await embed_document_task(ctx, "doc", "ent-conn-last", "proj-1")

    mock_failure.assert_awaited_once_with("proj-1")
    writes = [c for c in mock_db.query.await_args_list if "last_embed_error" in c.args[0]]
    assert writes, "no last_embed_error write reached the DB"
    assert "ConnectError" in writes[0].args[1]["err"]


@pytest.mark.asyncio
async def test_embed_document_task_4xx_is_not_transient():
    """A 4xx reaching the task is NOT retried — content rejections stay terminal.

    A 4xx content rejection is isolated per-string by _embed_texts_resilient's
    bisection (pinned in test_chunk_markdown.py) and never legitimately reaches
    this branch; widening the except tuple for TransportError must not swallow
    it into the retry loop, where it would burn the whole budget on strings the
    provider will never accept.
    """
    import httpx

    from jobs.tasks import embed_document_task

    class FakeRedis:
        async def get(self, key):
            return None
        async def delete(self, key):
            pass

    ctx = {"redis": FakeRedis(), "job_id": "embed:ent-4xx", "job_try": 1}

    mock_db = AsyncMock()
    mock_db.query = AsyncMock(return_value=None)

    with patch("embeddings._reembed", side_effect=_http_status_error(400)), \
         patch("embeddings._on_embed_failure", new_callable=AsyncMock) as mock_failure, \
         patch("embeddings._on_embed_success", new_callable=AsyncMock) as mock_success, \
         patch("doc_outline.refresh_outline", new_callable=AsyncMock), \
         patch("db.get_db", return_value=mock_db):
        with pytest.raises(httpx.HTTPStatusError):
            await embed_document_task(ctx, "doc", "ent-4xx", "proj-1")

    mock_failure.assert_not_awaited()
    mock_success.assert_not_awaited()


@pytest.mark.asyncio
async def test_embed_document_task_redefers_when_edit_lands_during_run():
    """An edit landing DURING the embed run re-defers the job — after success.

    arq holds the job key until the task returns, so the edit's enqueue
    collapsed to None (arq ≥0.26 dedup) and arq cannot pull a deferred job
    forward: the RUNNING task must pick the new deadline up itself. Order
    matters — _on_embed_success runs first, then Retry(defer≈remaining), with
    the retry counter reset so the re-defer burns no failure budget.
    """
    from arq.constants import retry_key_prefix
    from arq.worker import Retry

    from jobs.tasks import embed_document_task

    deadline_time = {"v": None}  # _reembed sets it — the edit lands mid-run
    deleted = []

    class FakeRedis:
        async def get(self, key):
            if key == "embed:deadline:ent-midrun" and deadline_time["v"] is not None:
                return str(deadline_time["v"]).encode()
            return None
        async def delete(self, key):
            deleted.append(key)

    ctx = {"redis": FakeRedis(), "job_id": "embed:ent-midrun", "job_try": 1, "max_tries": 4}

    async def reembed_then_edit(entity_type, entity_id, project_id):
        deadline_time["v"] = time.time() + 30

    with patch("embeddings._reembed", side_effect=reembed_then_edit), \
         patch("embeddings._on_embed_success", new_callable=AsyncMock) as mock_success:
        with pytest.raises(Retry) as ri:
            await embed_document_task(ctx, "doc", "ent-midrun", "proj-1")

    mock_success.assert_awaited_once_with("proj-1")
    assert 20_000 < ri.value.defer_score <= 30_000  # arq stores defer in ms
    assert retry_key_prefix + "embed:ent-midrun" in deleted


@pytest.mark.asyncio
async def test_embed_document_task_returns_plain_with_no_edit_during_run():
    """No deadline at the tail ⇒ plain return — the tail check never turns a
    finished embed into a spurious Retry."""
    from jobs.tasks import embed_document_task

    class FakeRedis:
        async def get(self, key):
            return None
        async def delete(self, key):
            pass

    ctx = {"redis": FakeRedis(), "job_id": "embed:ent-plain", "job_try": 1, "max_tries": 4}

    with patch("embeddings._reembed", new_callable=AsyncMock) as mock_reembed, \
         patch("embeddings._on_embed_success", new_callable=AsyncMock) as mock_success:
        result = await embed_document_task(ctx, "doc", "ent-plain", "proj-1")

    assert result is None
    mock_reembed.assert_awaited_once()
    mock_success.assert_awaited_once_with("proj-1")

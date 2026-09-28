"""B1 — silent-degradation fixes (plan phase-b-debt-payoff, DEC3).

Four real violations of the no-silent-degradation rule, each TDD'd here:
1. `_merge_candidates_for_reference` drops merge candidates on embed failure with a
   bare `except Exception: return []` — no log. The dedup gate and nearest-facts
   twins log with exc_info; this must too.
2. `search_exec`'s FTS-disjunct failure falls back to CONTAINS but never appends a
   warning — the model is never told recall dropped (the module's own contract and
   both layer catches DO append).
3. `transcribe_task`'s three early error exits (missing file_path, path traversal,
   file missing) set DB status 'error' but emit no WS — contradicting the sibling
   invariant on `_mark_error` (every error exit notifies the UI).
4. `on_transcription_complete` ends in `except Exception: logger.error` — a failed
   agent_configs lookup or enqueue means the user's configured auto-extraction
   silently never runs. Must leave an error note on the source document.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

# ─── 1. merge-candidate drop is logged ──────────────────────────────────────


async def test_embed_failure_logging_merge_candidates(test_db, project_with_doc, caplog):
    """A broken embed backend must not take the merge candidates away SILENTLY:
    the fallback to [] stays (best-effort by design), but a WARNING with the
    traceback is recorded — matching the dedup-gate and nearest-facts twins."""
    pid, _idx, _uid = project_with_doc
    from memory._candidates import _merge_candidates_for_reference

    from db import get_db

    db = await get_db()
    with patch("embeddings.embed_texts", new=AsyncMock(side_effect=RuntimeError("embed backend down"))), \
         caplog.at_level(logging.WARNING, logger="memory._candidates"):
        out = await _merge_candidates_for_reference(
            db, pid, "some-host", {"content": "material that would rank candidates"},
        )
    assert out == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "embed failure in merge-candidate selection logged nothing"
    assert any(r.exc_info for r in warnings), "the warning must carry the traceback (exc_info=True)"


# ─── 2. FTS fallback reports degraded recall ────────────────────────────────


async def test_fts_disjunct_failure_appends_degraded_warning():
    """When the FTS disjunct fails and the layer falls back to CONTAINS-only, the
    result must carry `search_degraded:fts` — the stemmer's inflected matches are
    GONE from recall and the model has to know that, exactly as the semantic and
    direct layer catches already report themselves."""
    from agent.search_exec import _search_materials_exec

    async def fake_query(stmt, params=None):
        s = stmt.lower()
        if "@0@" in s:
            raise RuntimeError("simulated: FTS index absent")
        if "limit 200" in s:
            return [{"id": "d1", "title": "doc", "content": "body",
                     "is_reference": False, "parent_id": None, "is_memory": False,
                     "updated_at": None, "sort_key": ""}]
        if "parent_id from documents" in s:
            return [{"id": "d1", "parent_id": None}]
        return []

    from retrieval import RetrievalResult

    with patch("retrieval.retrieve_context", new=AsyncMock(return_value=RetrievalResult(hits=[]))), \
         patch("agent.search_exec.get_db") as gdb, \
         patch("agent.search_exec.get_document_access",
               new=AsyncMock(return_value=True)):
        gdb.return_value.query = fake_query
        result = await _search_materials_exec(
            project_id="p1", user={"user_id": "u1"}, query="body", k=5,
        )
    assert result["hits"], "sanity: the CONTAINS fallback still returns the hit"
    assert "search_degraded:fts" in result.get("warnings", []), (
        f"FTS fallback is silent about lost recall: {result.get('warnings')}"
    )


# ─── 3. transcribe_task early exits notify the UI ───────────────────────────


async def _ref(ref_id: str, pid: str, **extra) -> None:
    from db import create_record

    payload = {
        "project_id": pid, "parent_id": "some-doc", "title": "audio.webm",
        "media_type": "audio", "content": "", "processing_status": "queued",
        "path": f"_ref/{ref_id}.md", "is_reference": True,
    }
    payload.update(extra)
    await create_record("documents", ref_id, payload)


async def _status(ref_id: str) -> str | None:
    from db import get_db

    db = await get_db()
    rows = await db.query(
        "SELECT processing_status FROM type::record('documents', $id)", {"id": ref_id},
    )
    return (rows[0] or {}).get("processing_status") if rows else None


async def _capture_error_events(ref_id: str, pid: str, body):
    """Run `body()` under a transcription_error capture; return captured events."""
    from event_bus import off, on

    events: list[dict] = []

    async def capture(**kw):
        events.append(kw)

    on("transcription_error", capture)
    try:
        await body()
        await asyncio.sleep(0.05)  # drain fire-and-forget emit
    finally:
        off("transcription_error", capture)
    return events


async def test_transcribe_missing_file_path_emits_error(test_db):
    """Exit 1 — missing file_path: status 'error' AND transcription_error emitted."""
    from jobs.tasks import transcribe_task

    ref_id, pid = "b1-tr-no-path", "b1-tr-proj"
    await _ref(ref_id, pid, file_path="")

    async def run():
        await transcribe_task({"job_try": 1, "max_tries": 2}, "test-user", ref_id)

    events = await _capture_error_events(ref_id, pid, run)
    assert await _status(ref_id) == "error"
    assert len(events) == 1 and events[0]["reference_id"] == ref_id, events


async def test_transcribe_path_traversal_emits_error(test_db):
    """Exit 2 — path traversal: status 'error' AND transcription_error emitted."""
    from jobs.tasks import transcribe_task

    ref_id, pid = "b1-tr-traversal", "b1-tr-proj"
    await _ref(ref_id, pid, file_path="../../etc/passwd")

    async def run():
        await transcribe_task({"job_try": 1, "max_tries": 2}, "test-user", ref_id)

    events = await _capture_error_events(ref_id, pid, run)
    assert await _status(ref_id) == "error"
    assert len(events) == 1 and events[0]["reference_id"] == ref_id, events


async def test_transcribe_file_missing_on_disk_emits_error(test_db):
    """Exit 3 — file_path present but no file at it: status 'error' AND
    transcription_error emitted."""
    from jobs.tasks import transcribe_task

    ref_id, pid = "b1-tr-no-file", "b1-tr-proj"
    await _ref(ref_id, pid, file_path="audio/b1-nonexistent.webm")

    async def run():
        await transcribe_task({"job_try": 1, "max_tries": 2}, "test-user", ref_id)

    events = await _capture_error_events(ref_id, pid, run)
    assert await _status(ref_id) == "error"
    assert len(events) == 1 and events[0]["reference_id"] == ref_id, events


# ─── 4. extractor hook failure leaves a trace ───────────────────────────────


async def test_extractor_hook_failure_creates_error_note(test_db):
    """A failed enqueue inside on_transcription_complete must leave an error note
    on the source document — the user configured auto-extraction and it silently
    never ran. The note is the operator-visible record; the log alone is not."""
    from pipeline.extractor.runner import on_transcription_complete

    from db import create_record, get_db

    pid = "b1-ext-proj"
    doc_id = "b1-ext-doc"
    ref_id = "b1-ext-ref"
    config_doc_id, target_doc_id = "b1-ext-cfg", "b1-ext-tgt"

    await create_record("documents", doc_id, {
        "project_id": pid, "parent_id": None, "title": "Doc",
        "content": "", "path": f"{doc_id}.md", "is_index": True,
    })
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": doc_id, "title": "Ref",
        "content": "transcribed text", "path": f"_ref/{ref_id}.md",
        "is_reference": True, "media_type": "audio",
    })
    await create_record("documents", config_doc_id, {
        "project_id": pid, "parent_id": None, "title": "Cfg",
        "content": "", "path": "cfg.md", "is_index": False,
    })
    await create_record("documents", target_doc_id, {
        "project_id": pid, "parent_id": None, "title": "Tgt",
        "content": "", "path": "tgt.md", "is_index": False,
    })
    db = await get_db()
    await db.query(
        "CREATE agent_configs CONTENT { document_id: $did, config_doc_id: $cid, "
        "target_doc_id: $tid, project_id: $pid, trigger_event: 'transcription_complete', deleted_at: NONE }",
        {"did": doc_id, "cid": config_doc_id, "tid": target_doc_id, "pid": pid},
    )

    async def boom(name, *args, **kwargs):
        raise RuntimeError("queue down")

    with patch("jobs.pool.enqueue", side_effect=boom):
        await on_transcription_complete(reference_id=ref_id, project_id=pid, user_id="user-1")

    rows = await db.query(
        "SELECT meta::id(id) AS id FROM chat_sessions "
        "WHERE document_id = $did AND is_note = true AND title = 'Pipeline Error'",
        {"did": doc_id},
    )
    assert rows, "extractor hook failure left no error note — auto-extraction silently skipped"

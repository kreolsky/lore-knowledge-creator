"""Auto-backup tasks — see SYSTEM: auto_backup (the arq half).

# INVARIANT: every backup task that MUST be registered on the default worker.
# Why: arq silently drops a job whose function is not registered on the polled worker
# ("enqueued but never run", no error). A new auto_backup_*_task added here but
# forgotten in WorkerSettings.functions must crash the worker at startup (see
# validate_queue_config in jobs/worker.py) instead of silently never consuming its jobs.
# This frozenset is the canonical cross-check.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def auto_backup_loss_task(
    ctx,
    document_id: str,
    new_content: str,
    baseline_content: str | None,
    baseline_tables_json: str | None,
) -> None:
    from auto_backup import maybe_backup_on_content_loss
    await maybe_backup_on_content_loss(
        document_id, new_content,
        baseline_content=baseline_content,
        baseline_tables_json=baseline_tables_json,
    )


async def auto_backup_handoff_task(
    ctx,
    document_id: str,
    content: str,
    from_user_id: str,
    from_user_name: str,
    tables_json: str | None,
) -> None:
    """Persist the editor-handoff snapshot captured on the collab push path.

    The push path captured `content` + `tables_json` (the doc state BEFORE the new
    editor's first edit) synchronously; the actual checkpoint write runs here, off the
    hot path. Cross-process: no in-memory hash hint, so dedup relies on the DB query.
    """
    from auto_backup import maybe_backup_on_editor_handoff
    await maybe_backup_on_editor_handoff(
        document_id, content, from_user_id=from_user_id, from_user_name=from_user_name,
        tables_json=tables_json,
    )


async def auto_backup_open_task(
    ctx,
    document_id: str,
    content: str,
    is_reference: bool,
    content_hash: str,
    tables_json: str | None,
) -> None:
    """Session-start safety backup on document open (content-hash dedup)."""
    from auto_backup import maybe_backup_on_open
    await maybe_backup_on_open(
        document_id, content, tables_json=tables_json,
        is_reference=is_reference, _precomputed_hash=content_hash,
    )


async def auto_backup_last_session_task(
    ctx,
    document_id: str,
    content: str,
    editor_id: str,
    editor_name: str,
    tables_json: str | None,
) -> None:
    """Upsert the per-user "last session" checkpoint (doc state at session end).

    Triggered off the collab hot path at editor-handoff / leave / idle-reap for an
    editor in session._has_pushed. job_id=f"last-session:{doc}:{editor_id}" keeps
    keep_result=0 reusable (backend.md arq note); failures surface via this logger
    since keep_result=0 hides them from the arq result store.
    """
    from auto_backup import upsert_last_session_backup
    try:
        await upsert_last_session_backup(
            document_id, content, editor_id, editor_name, tables_json=tables_json,
        )
    except Exception:
        logger.warning("Failed to upsert last-session backup for doc=%s editor=%s",
                       document_id, editor_id, exc_info=True)


BACKUP_TASK_NAMES = frozenset({
    "auto_backup_loss_task",
    "auto_backup_handoff_task",
    "auto_backup_open_task",
    "auto_backup_last_session_task",
})

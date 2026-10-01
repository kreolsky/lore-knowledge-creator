"""arq task functions — the compatibility surface over the split task package.

Task signatures follow arq convention: async def task(ctx, *args).

# This package was split per the domain map (R1-jobs) — media (STT/convert),
# extract, embed, thumbnails, backup, telemetry, cp_migrations, thinning, comfy,
# schedules — and THIS module is the re-export shim that keeps `jobs.tasks` the
# single import surface. Load-bearing importers that MUST keep working unchanged:
# jobs/worker.py (registers every function on the two worker classes),
# pipeline/core/registry.py (`from jobs.tasks import extract_task`),
# backend/scripts/cp_prod_migrate.py (backfill), and the test suite (symbol
# imports + the string-patch targets live on the submodules).
# INVARIANT: arq resolves tasks by coroutine __name__, never by module path —
# every function moved into this package keeps its name verbatim.
# Why: the wire (queue entries) carries the function NAME, so a rename would
# strand every in-flight job and break the name-based enqueue sites; the split
# must be invisible on the wire. WorkerSettings' function lists and the
# string-patch targets in the test suite are likewise name-based, which is what
# lets this re-export shim keep them all working unchanged.
"""
from jobs.tasks._dead_letter import dead_letter
from jobs.tasks.backup import (
    BACKUP_TASK_NAMES,
    auto_backup_handoff_task,
    auto_backup_last_session_task,
    auto_backup_loss_task,
    auto_backup_open_task,
)
from jobs.tasks.comfy import COMFY_TASK_NAMES, generate_image_task
from jobs.tasks.cp_migrations import backfill_cp_blobs_task, nullout_inline_content_task
from jobs.tasks.embed import EMBED_MAX_TRIES, embed_document_task, embed_sweep_task
from jobs.tasks.extract import extract_task
from jobs.tasks.help import help_seed_task, help_sweep_task
from jobs.tasks.media import (
    _post_to_converter,
    _read_docx_bytes,
    convert_docx_task,
    convert_pdf_task,
    transcribe_task,
)
from jobs.tasks.schedules import pipeline_schedule_tick_task
from jobs.tasks.telemetry import TELEMETRY_RETENTION_DAYS, telemetry_retention_task
from jobs.tasks.thinning import _flush_thin_deletes, thin_auto_checkpoints_task
from jobs.tasks.thumbnails import thumbnail_task
from jobs.tasks.widget_extract import widget_extract_task

__all__ = [
    # media (STT/convert)
    "transcribe_task",
    "convert_docx_task",
    "convert_pdf_task",
    "_post_to_converter",
    "_read_docx_bytes",
    # extractor
    "extract_task",
    # embeddings
    "embed_document_task",
    "embed_sweep_task",
    "EMBED_MAX_TRIES",
    # thumbnails
    "thumbnail_task",
    # Lore guide
    "help_seed_task",
    "help_sweep_task",
    # auto_backup
    "auto_backup_loss_task",
    "auto_backup_handoff_task",
    "auto_backup_open_task",
    "auto_backup_last_session_task",
    "BACKUP_TASK_NAMES",
    # telemetry
    "telemetry_retention_task",
    "TELEMETRY_RETENTION_DAYS",
    # cp migrations
    "backfill_cp_blobs_task",
    "nullout_inline_content_task",
    # thinning
    "thin_auto_checkpoints_task",
    "_flush_thin_deletes",
    # comfy
    "generate_image_task",
    "COMFY_TASK_NAMES",
    # schedules
    "pipeline_schedule_tick_task",
    # widget batch extract (CIR T2)
    "widget_extract_task",
    # shared terminal-failure contract
    "dead_letter",
]

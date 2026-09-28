"""One-shot backfill: file_meta.duration_sec for existing audio references.

New audio uploads now store duration_sec (and remux duration-less WebM so the
container reports its own duration — see files_util.normalize_audio_upload_path).
References uploaded BEFORE that change have no duration_sec, so the status badge
renders an empty label (ref-utils StatusBadge) and the player's total time is
unknown. This script lights both up WITHOUT rewriting any stored file — the
write-once + immutable-cache INVARIANT (files_serve.REFERENCE_FILE_CACHE_CONTROL)
forbids in-place file replacement, so only the metadata is patched.

Duration-less WebM containers (MediaRecorder output) report N/A to ffprobe, so
for those a full decode (`ffmpeg -i f -f null -`) is the only way to time them.

Idempotent + safe to re-run: only touches rows whose file_meta lacks duration_sec.

Usage (inside the prod backend container):
    docker compose -p lore -f docker-compose.prod.yml exec -T backend \\
        python /app/scripts/backfill_audio_duration.py

Pass --dry-run to probe + log without writing.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from files_util import probe_duration_sec

from config import STORAGE_PATH
from db import get_db

logger = logging.getLogger("backfill_audio_duration")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ffmpeg full-decode progress lines carry time=HH:MM:SS.ss; the LAST one is the
# total duration of a duration-less container (ffprobe reports N/A for those).
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


async def full_decode_duration(path: Path) -> float | None:
    """Time a duration-less container by fully decoding it (`ffmpeg -f null -`).

    ffprobe returns None for MediaRecorder WebM (no Duration element); only a full
    decode yields the real length. Returns None on any failure.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-i", str(path), "-f", "null", "-",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    matches = _TIME_RE.findall(err.decode("utf-8", "replace"))
    if not matches:
        return None
    h, m, s = matches[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


async def _backfill_one(
    db, dry_run: bool, storage_root: Path, r: dict
) -> str:
    """Process one audio reference row; return its outcome bucket.

    Mutates only file_meta (never the stored file — REFERENCE_FILE_CACHE_CONTROL
    is immutable). Tries the fast container-Duration path, then falls back to a
    full decode for duration-less WebM. Returns "updated" | "already" | "failed".
    """
    rid = str(r["id"])
    meta = r.get("file_meta") or {}
    if meta.get("duration_sec") is not None:
        return "already"

    rel = r.get("file_path") or ""
    if not rel:
        logger.warning("[%s] no file_path — skipping", rid)
        return "failed"
    abs_path = (STORAGE_PATH / rel).resolve()
    if not abs_path.is_relative_to(storage_root) or not abs_path.is_file():
        logger.warning("[%s] file missing on disk (%s) — skipping", rid, rel)
        return "failed"

    # Fast path (container carries a Duration); fall back to a full decode for
    # the duration-less WebM case.
    dur = await probe_duration_sec(abs_path)
    if dur is None:
        dur = await full_decode_duration(abs_path)
    if dur is None or dur <= 0:
        logger.warning("[%s] could not determine duration — skipping", rid)
        return "failed"

    logger.info("[%s] duration=%.3fs", rid, dur)
    if not dry_run:
        await db.query(
            "UPDATE type::record('documents', $id) SET file_meta = $fm",
            {"id": rid, "fm": {**meta, "duration_sec": dur}},
        )
    return "updated"


async def main(dry_run: bool) -> None:
    db = await get_db()
    rows = await db.query(
        "SELECT meta::id(id) AS id, file_path, file_meta FROM documents "
        "WHERE is_reference = true AND media_type = 'audio' AND deleted_at IS NONE"
    )
    rows = rows or []
    logger.info("Audio references: %d (dry_run=%s)", len(rows), dry_run)

    storage_root = STORAGE_PATH.resolve()
    counts = {"updated": 0, "already": 0, "failed": 0}
    for r in rows:
        counts[await _backfill_one(db, dry_run, storage_root, r)] += 1

    logger.info(
        "Done — updated: %d, already-had-duration: %d, failed/skipped: %d",
        counts["updated"], counts["already"], counts["failed"],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill file_meta.duration_sec for audio references")
    parser.add_argument("--dry-run", action="store_true", help="probe + log without writing to the DB")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))

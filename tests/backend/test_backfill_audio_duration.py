"""Backfill script: file_meta.duration_sec for pre-existing audio references.

Validates the one-shot backfill (backend/scripts/backfill_audio_duration.py) end
to end — the full-decode duration fallback for duration-less WebM (ffprobe reports
N/A) and the DB merge that restores duration_sec without touching stored files.
"""

import importlib.util

import pytest
from test_adts_remux import WEBM_BYTES, WEBM_NO_DUR, WEBM_WITH_DUR

_SCRIPT = "/app/scripts/backfill_audio_duration.py"


def _load():
    spec = importlib.util.spec_from_file_location("backfill_audio_duration", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _clear_audio_refs(db) -> None:
    """The backfill iterates EVERY audio ref in the DB; without the `client`
    fixture there is no per-test reset, so clear audio refs first to keep the
    run deterministic (only the ref this test creates is processed)."""
    await db.query(
        "DELETE documents WHERE is_reference = true AND media_type = 'audio'"
    )


# ─── Unit: full_decode_duration (duration-less container fallback) ───────────


@pytest.mark.asyncio
async def test_full_decode_duration_times_durationless_webm(tmp_path):
    # ffprobe reports N/A for this container; only a full decode yields the length.
    mod = _load()
    p = tmp_path / "nodur.webm"
    p.write_bytes(WEBM_NO_DUR)
    dur = await mod.full_decode_duration(p)
    assert dur is not None and 5.0 < dur < 7.0   # 300 opus frames ≈ 6s


@pytest.mark.asyncio
async def test_full_decode_duration_none_for_garbage(tmp_path):
    mod = _load()
    p = tmp_path / "garbage.webm"
    p.write_bytes(WEBM_BYTES)
    assert await mod.full_decode_duration(p) is None


# ─── Integration: main() restores duration_sec without rewriting the file ─────


@pytest.mark.asyncio
async def test_backfill_restores_duration_sec_for_ref_missing_it(tmp_path):
    import files_util

    from db import fetch_one, get_db

    mod = _load()

    # A real audio ref with a seekable WebM on disk (save_upload writes duration_sec).
    db = await get_db()
    await _clear_audio_refs(db)
    src = tmp_path / "in.webm"
    src.write_bytes(WEBM_WITH_DUR)
    ref_id, _ = await files_util.save_upload(
        None, "audio/webm", "clip.webm", "ptest_backfill", "ptest_backfill-host",
        title="clip", media_type="audio", processing_status=None, src_path=src,
    )
    ref = await fetch_one("documents", ref_id)
    assert ref["file_meta"]["duration_sec"] > 0

    stored_before = ref["file_path"]

    # Simulate an OLD ref uploaded before the change: strip duration_sec.
    stripped = {k: v for k, v in ref["file_meta"].items() if k != "duration_sec"}
    await db.query(
        "UPDATE type::record('documents', $id) SET file_meta = $fm",
        {"id": ref_id, "fm": stripped},
    )
    assert "duration_sec" not in (await fetch_one("documents", ref_id))["file_meta"]

    # Backfill (not dry-run) — runs against the test DB the session owns.
    await mod.main(dry_run=False)

    after = await fetch_one("documents", ref_id)
    assert after["file_meta"]["duration_sec"] > 0
    # The stored file is never rewritten (write-once + immutable-cache invariant).
    assert after["file_path"] == stored_before


@pytest.mark.asyncio
async def test_backfill_dry_run_does_not_write(tmp_path):
    import files_util

    from db import fetch_one, get_db

    mod = _load()

    db = await get_db()
    await _clear_audio_refs(db)
    src = tmp_path / "in.webm"
    src.write_bytes(WEBM_WITH_DUR)
    ref_id, _ = await files_util.save_upload(
        None, "audio/webm", "clip.webm", "ptest_backfill_dry", "ptest_backfill_dry-host",
        title="clip", media_type="audio", processing_status=None, src_path=src,
    )
    ref = await fetch_one("documents", ref_id)
    stripped = {k: v for k, v in ref["file_meta"].items() if k != "duration_sec"}
    await db.query(
        "UPDATE type::record('documents', $id) SET file_meta = $fm",
        {"id": ref_id, "fm": stripped},
    )

    await mod.main(dry_run=True)

    # Dry-run: duration_sec stays absent.
    assert "duration_sec" not in (await fetch_one("documents", ref_id))["file_meta"]

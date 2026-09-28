"""Windowed portions — the consolidation cursor walks INSIDE a reference
(plan `.kilo/plans/1786300000000-memory-windowed-portions.md`).

The unit of consolidation was ONE reference; now it is a WINDOW of one reference.
`content` is sliced on the way OUT (the window belongs to the tool, not the data),
and a third cursor field `mem_window_done` records how many windows of the current
reference are consumed — `mem_consolidated_at` is written only when the LAST
window completes. The slicing MUST match the bench that produced the measured fact
yield (`bench_arm.split`), or the bench acceptance will not reproduce.

These pin:
  - the pure slicing math (count, seams, overlap, whitespace landing, passthrough);
  - the slicer equals the bench oracle verbatim (the acceptance contract);
  - serve slices content; the cursor advances per window and consumes on the last;
  - a retried `next_reference` is idempotent (stamped:false, no skipped window);
  - clearing resets the WINDOW cursor too (Risk #1 — assert the FIELD, not "looks fine");
  - the in-order stamp guard still refuses with windows in play;
  - progress reports windows alongside references.
"""
import hashlib
import math
import secrets

import pytest
import pytest_asyncio
from helpers import pin_stt_url

# ════════════════════════════════════════════════════════════════════════════
# Pure slicing math — memory/_window.py
# ════════════════════════════════════════════════════════════════════════════

def _bench_snap(text: str, pos: int) -> int:
    """The bench's whitespace snap (bench_arm._snap), inlined as the ORACLE.

    The production slicer must reproduce this byte-for-byte; copying it here means a
    drift in the slicer fails this test, not just the bench acceptance a session later.
    """
    if pos <= 0:
        return 0
    if pos >= len(text):
        return len(text)
    for d in range(121):
        for p in (pos - d, pos + d):
            if 0 < p < len(text) and text[p].isspace():
                return p + 1
    return pos


def _bench_split(text: str, size: int, overlap: int) -> list[tuple[int, int, str]]:
    """The bench's split (bench_arm.split) as (from, to, text) — the oracle."""
    n = max(1, round(len(text) / size))
    if n == 1:
        return [(0, len(text), text)]
    base, half = len(text) / n, overlap // 2
    out = []
    for i in range(n):
        a = _bench_snap(text, round(i * base) - half) if i else 0
        b = _bench_snap(text, round((i + 1) * base) + half) if i < n - 1 else len(text)
        out.append((a, b, text[a:b]))
    return out


def _words(n_chars: int) -> str:
    """Whitespace-rich text of ~n_chars (words separated by spaces, lines by newlines)
    so the snap always finds a boundary near a seam."""
    out, total = [], 0
    i = 0
    while total < n_chars:
        i += 1
        piece = f"word{i}. " if i % 12 == 0 else f"w{i} "
        out.append(piece)
        total += len(piece)
    return "".join(out)


def test_window_count_passthrough_for_short_content():
    from memory._window import WINDOW_CHARS, window_count

    assert window_count(0) == 1
    assert window_count(100) == 1
    assert window_count(WINDOW_CHARS) == 1
    # n = round(len/window): the n=2 boundary is round(1.5)=2 → len 18_000, NOT 12_001
    # (round(1.00008)=1, still a one-window passthrough — the bench's `n` formula).
    assert window_count(WINDOW_CHARS + 1) == 1
    assert window_count(17_999) == 1
    assert window_count(18_000) == 2


def test_window_count_uses_round_formula():
    """Count is round(len/window) — the bench's `n`, not a ceil/step count."""
    from memory._window import WINDOW_CHARS, window_count

    assert window_count(48_524) == round(48_524 / WINDOW_CHARS)  # 4
    assert window_count(48_524) == 4
    assert window_count(25_000) == round(25_000 / WINDOW_CHARS)  # 2
    assert window_count(25_000) == 2
    assert window_count(40_000) == 3


def test_slice_windows_passthrough_short():
    from memory._window import slice_windows

    text = "short reference body"
    assert slice_windows(text) == [(0, len(text), text)]


def test_slice_windows_count_matches_window_count():
    from memory._window import slice_windows, window_count

    for n in (12_500, 25_000, 40_000, 48_524, 60_000):
        text = _words(n)
        wins = slice_windows(text)
        assert len(wins) == window_count(len(text)), f"len={len(text)}"


def test_slice_windows_full_coverage_ordered():
    """Windows start at 0, end at len(text), and never leave a gap."""
    from memory._window import slice_windows

    text = _words(40_000)
    wins = slice_windows(text)
    assert wins[0][0] == 0
    assert wins[-1][1] == len(text)
    for (a, b, _), (a2, _b2, _t2) in zip(wins, wins[1:]):
        # overlap ⇒ the next window starts at or before this one ends (never a gap)
        assert a2 <= b, f"gap between {b} and {a2}"
        assert a2 < b, "interior seam must overlap (the seam-twin guarantee)"


def test_slice_windows_cuts_land_on_whitespace():
    """Every interior window starts just AFTER a whitespace char (the snap contract)."""
    from memory._window import slice_windows

    text = _words(40_000)
    wins = slice_windows(text)
    for i, (a, _b, _t) in enumerate(wins):
        if i == 0:
            assert a == 0
            continue
        assert a == 0 or text[a - 1].isspace(), (
            f"window {i} starts at {a} mid-word (prev={text[a - 3:a + 2]!r})"
        )


def test_slice_windows_equals_the_bench_oracle():
    """The production slicer MUST reproduce bench_arm.split exactly — this is the
    acceptance contract (a real bench run reproduces the measured yield only if the
    production slicing is the bench slicing)."""
    from memory._window import OVERLAP_CHARS, WINDOW_CHARS, slice_windows

    for n in (12_500, 25_000, 40_000, 48_524, 73_000):
        text = _words(n)
        expected = _bench_split(text, WINDOW_CHARS, OVERLAP_CHARS)
        got = slice_windows(text)
        assert got == expected, f"divergence from bench at {n} chars"


# ════════════════════════════════════════════════════════════════════════════
# DB-backed cursor — serve/advance/complete, idempotency, clear, order, progress
# ════════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture(autouse=True)
async def _clean_memory_space(project_with_doc, test_db):
    """Hermetic: the queue-walk queries would otherwise see references left by other
    tests under the project's index doc. Wipe references + memory facts first."""
    pid = project_with_doc[0]
    await test_db.query(
        "DELETE documents WHERE project_id = $pid AND is_memory = true", {"pid": pid},
    )
    await test_db.query(
        "DELETE documents WHERE project_id = $pid AND is_reference = true", {"pid": pid},
    )


async def _make_ref(test_db, project_id, host_id, slug, *, chars, created_at):
    """A live, readable reference under `host_id` with explicit created_at (deterministic
    oldest-first ordering) and `chars` of whitespace-rich content."""
    from db import create_record

    ref_id = f"win-{slug}"
    await create_record("documents", ref_id, {
        "project_id": project_id, "parent_id": host_id, "title": f"portion {slug}",
        "content": _words(chars), "path": f"_ref/{ref_id}.md", "is_index": False,
        "is_reference": True, "media_type": "markdown", "processing_status": "ready",
        "created_at": created_at,
    })
    return ref_id


async def _run(project_with_doc):
    from memory.run_key import mint_memory_run_key

    pid, host, uid = project_with_doc
    run = await mint_memory_run_key(user_id=uid, project_id=pid)
    return pid, host, uid, run.run_id


# ─── HTTP tool-api helpers (real Pi surface) ────────────────────────────────


async def _make_agent_key(test_db, user_id, project_id, *, auto_apply=False):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"win-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": "",
        "token_hash": token_hash, "label": "agent", "capabilities": ["agent"],
        "auto_apply": auto_apply,
    })
    return token


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_http_loop_advances_windows_with_index(
    client, test_db, admin_user, project_with_doc,
):
    """Real HTTP through the Pi tool-api: consolidate_memory serves window 0, and
    next_reference accepts `completed_window_index` to advance — then a retried call
    (same index) is `stamped: false` over the wire. Pins the route + model wiring
    end-to-end (the DB-backed tests above cover the function logic)."""
    from datetime import datetime, timezone

    pid, host, admin_uid = project_with_doc
    tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)
    ref = await _make_ref(test_db, pid, host, "http", chars=40_000,
                          created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    first = await client.post(
        "/api/tool/consolidate_memory",
        json={"target_doc_id": host}, headers=_hdr(tok),
    )
    assert first.status_code == 200, first.text
    body = first.json()
    run_id = body["run_id"]
    assert body["reference"]["window"]["total"] == 3
    assert body["reference"]["window"]["index"] == 0

    adv = await client.post("/api/tool/next_reference", json={
        "run_id": run_id, "completed_reference_id": ref,
        "completed_window_index": 0,
    }, headers=_hdr(tok))
    assert adv.status_code == 200, adv.text
    assert adv.json()["stamped"] is True
    assert adv.json()["reference"]["window"]["index"] == 1

    # retried call (lost response) over the wire: idempotent, re-serves window 1
    retry = await client.post("/api/tool/next_reference", json={
        "run_id": run_id, "completed_reference_id": ref,
        "completed_window_index": 0,
    }, headers=_hdr(tok))
    assert retry.status_code == 200, retry.text
    assert retry.json()["stamped"] is False
    assert retry.json()["reference"]["window"]["index"] == 1


@pytest.mark.asyncio
async def test_short_reference_served_whole_as_one_window(project_with_doc, test_db):
    """A reference shorter than one window is passthrough: one window, full content,
    and a single stamp consumes it — the change is invisible for short material."""
    from datetime import datetime, timezone

    from memory.task_builder import build_consolidation_task, next_reference

    pid, host, _uid, run_id = await _run(project_with_doc)
    ref = await _make_ref(test_db, pid, host, "short", chars=500,
                          created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    portion = await build_consolidation_task(
        project_id=pid, target_doc_id=host, run_id=run_id,
    )
    assert portion["reference"]["id"] == ref
    full = await _raw_content(test_db, ref)
    assert portion["reference"]["window"] == {
        "index": 0, "total": 1, "from": 0, "to": len(full),
    }
    # content is the WHOLE reference, RAW (the slicer does not strip — F1: it must match
    # _progress's window count and the bench oracle, both of which use raw content).
    assert portion["reference"]["content"] == full

    nxt = await next_reference(
        project_id=pid, run_id=run_id, completed_reference_id=ref,
        completed_window_index=0,
    )
    assert nxt["stamped"] is True
    assert nxt["complete"] is True  # consumed in one stamp
    assert await _is_consumed(test_db, ref)


@pytest.mark.asyncio
async def test_long_reference_served_in_windows(project_with_doc, test_db):
    """A ~40k reference yields 3 windows; the first portion is a SLICE (not the whole),
    and the served text equals the slicer's window 0."""
    from datetime import datetime, timezone

    from memory._window import slice_windows
    from memory.task_builder import build_consolidation_task

    pid, host, _uid, run_id = await _run(project_with_doc)
    ref = await _make_ref(test_db, pid, host, "long", chars=40_000,
                          created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    full = await _raw_content(test_db, ref)
    expected = slice_windows(full)[0]

    portion = await build_consolidation_task(
        project_id=pid, target_doc_id=host, run_id=run_id,
    )
    win = portion["reference"]["window"]
    assert win["total"] == 3
    assert win["index"] == 0
    assert (win["from"], win["to"]) == (expected[0], expected[1])
    assert portion["reference"]["content"] == expected[2]


@pytest.mark.asyncio
async def test_stamp_walks_windows_and_consumes_on_last(project_with_doc, test_db):
    """The loop: window0 → stamp → window1 (stamped) → stamp → window2 → stamp →
    complete. mem_consolidated_at is written ONLY on the last window."""
    from datetime import datetime, timezone

    from memory.task_builder import build_consolidation_task, next_reference

    pid, host, _uid, run_id = await _run(project_with_doc)
    ref = await _make_ref(test_db, pid, host, "walk", chars=40_000,
                          created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    portion = await build_consolidation_task(
        project_id=pid, target_doc_id=host, run_id=run_id,
    )
    total = portion["reference"]["window"]["total"]
    seen_indexes = [portion["reference"]["window"]["index"]]
    for idx in range(total):
        nxt = await next_reference(
            project_id=pid, run_id=run_id, completed_reference_id=ref,
            completed_window_index=idx,
        )
        assert nxt["stamped"] is True, f"stamp at window {idx} must be progress"
        if idx < total - 1:
            assert nxt["reference"]["id"] == ref  # same reference, next window
            seen_indexes.append(nxt["reference"]["window"]["index"])
        else:
            assert nxt["complete"] is True
    assert seen_indexes == list(range(total))
    assert await _is_consumed(test_db, ref)
    # the window cursor is cleared once consumed (not left at a stale index)
    assert (await _window_done(test_db, ref)) is None


@pytest.mark.asyncio
async def test_retried_stamp_is_idempotent_and_does_not_skip(project_with_doc, test_db):
    """A next_reference response lost AFTER the cursor advanced: the retried call
    (same completed_window_index) is stamped:false and re-serves the current window —
    never advancing twice, never skipping a window (the option-B contract)."""
    from datetime import datetime, timezone

    from memory.task_builder import build_consolidation_task, next_reference

    pid, host, _uid, run_id = await _run(project_with_doc)
    ref = await _make_ref(test_db, pid, host, "retry", chars=40_000,
                          created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    await build_consolidation_task(project_id=pid, target_doc_id=host, run_id=run_id)

    first = await next_reference(  # advances window 0 → 1
        project_id=pid, run_id=run_id, completed_reference_id=ref,
        completed_window_index=0,
    )
    assert first["stamped"] is True and first["reference"]["window"]["index"] == 1

    retry = await next_reference(  # lost response → retried with the SAME index
        project_id=pid, run_id=run_id, completed_reference_id=ref,
        completed_window_index=0,
    )
    assert retry["stamped"] is False, "a retried stamp must not be progress"
    assert retry["reference"]["window"]["index"] == 1, "must re-serve window 1, not skip"
    assert await _window_done(test_db, ref) == 1, "the cursor did not double-advance"

    # the run still advances normally afterwards
    fwd = await next_reference(
        project_id=pid, run_id=run_id, completed_reference_id=ref,
        completed_window_index=1,
    )
    assert fwd["stamped"] is True
    assert fwd["reference"]["window"]["index"] == 2


@pytest.mark.asyncio
async def test_bogus_window_index_is_a_safe_noop(project_with_doc, test_db):
    """An out-of-range window index (agent mistake) does not corrupt the cursor: it is
    treated as already-past and is a stamped:false no-op, re-serving the current window."""
    from datetime import datetime, timezone

    from memory.task_builder import build_consolidation_task, next_reference

    pid, host, _uid, run_id = await _run(project_with_doc)
    ref = await _make_ref(test_db, pid, host, "bogus", chars=40_000,
                          created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    await build_consolidation_task(project_id=pid, target_doc_id=host, run_id=run_id)
    bogus = await next_reference(
        project_id=pid, run_id=run_id, completed_reference_id=ref,
        completed_window_index=99,
    )
    assert bogus["stamped"] is False
    assert bogus["reference"]["window"]["index"] == 0
    # The bogus no-op wrote nothing — mem_window_done stays at its initial NONE
    # (the serve reads NONE as window 0; the stored field is never zeroed to 0).
    assert await _window_done(test_db, ref) is None


@pytest.mark.asyncio
async def test_clear_resets_the_window_cursor_field(project_with_doc, test_db):
    """Risk #1: a window cursor that survives a clear silently drops material. Reopen
    must write NONE to mem_window_done ALONGSIDE the two stamps — assert the FIELD,
    not that a re-run 'looks fine'."""
    from datetime import datetime, timezone

    from memory.task_builder import (
        build_consolidation_task,
        next_reference,
        reopen_consolidation,
    )

    pid, host, _uid, run_id = await _run(project_with_doc)
    ref = await _make_ref(test_db, pid, host, "clear", chars=40_000,
                          created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    await build_consolidation_task(project_id=pid, target_doc_id=host, run_id=run_id)
    await next_reference(project_id=pid, run_id=run_id, completed_reference_id=ref,
                         completed_window_index=0)
    assert await _window_done(test_db, ref) == 1  # mid-walk

    result = await reopen_consolidation(project_id=pid, target_doc_id=host)
    assert result["cleared"] == 1

    # ALL THREE fields cleared together.
    assert await _window_done(test_db, ref) is None
    assert await _is_consumed(test_db, ref) is False
    row = await _raw_row(test_db, ref)
    assert row.get("mem_consolidated_run") is None

    # The next run re-serves window 0 — not window 2 (no silent drop).
    again = await build_consolidation_task(
        project_id=pid, target_doc_id=host, run_id=run_id,
    )
    assert again["reference"]["window"]["index"] == 0


@pytest.mark.asyncio
async def test_reprocess_resets_the_window_cursor_field(
    project_with_doc, test_db, monkeypatch,
):
    """Risk #1 on the THIRD cursor writer: reprocess wipes `content` and re-imports, so
    a surviving `mem_window_done` would resume the NEW transcript at window 2 and never
    read windows 0-1 — the same silent drop `reopen_consolidation` guards against.
    Assert the FIELD, then that the next portion re-serves window 0."""
    from datetime import datetime, timezone

    import files_service
    from memory.task_builder import build_consolidation_task, next_reference

    # No STT dispatch: this test is about the cursor write, not the import pipeline.
    pin_stt_url(monkeypatch, "")

    pid, host, uid, run_id = await _run(project_with_doc)
    ref = await _make_ref(test_db, pid, host, "reproc", chars=40_000,
                          created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    await test_db.query(
        "UPDATE type::record('documents', $id) SET media_type = 'audio', "
        "file_path = 'uploads/reproc.m4a'",
        {"id": ref},
    )
    await build_consolidation_task(project_id=pid, target_doc_id=host, run_id=run_id)
    await next_reference(project_id=pid, run_id=run_id, completed_reference_id=ref,
                         completed_window_index=0)
    assert await _window_done(test_db, ref) == 1  # mid-walk

    from db import fetch_one
    row = await fetch_one("documents", ref)
    await files_service.reprocess_reference(ref, row, user_id=uid)

    assert await _window_done(test_db, ref) is None
    assert await _is_consumed(test_db, ref) is False

    # And the walk restarts from the top once the new content lands.
    await test_db.query(
        "UPDATE type::record('documents', $id) SET content = $c, "
        "processing_status = 'ready'",
        {"id": ref, "c": _words(40_000)},
    )
    again = await build_consolidation_task(
        project_id=pid, target_doc_id=host, run_id=run_id,
    )
    assert again["reference"]["window"]["index"] == 0


@pytest.mark.asyncio
async def test_in_order_guard_still_refuses_with_windows(project_with_doc, test_db):
    """The in-order stamp guard compares against the oldest UNCONSUMED reference; a
    partially-walked reference is still unconsumed, so the guard keeps refusing an
    out-of-order completion (verify, not assume — Risk #2)."""
    from datetime import datetime, timezone

    from fastapi import HTTPException
    from memory.task_builder import build_consolidation_task, next_reference

    pid, host, _uid, run_id = await _run(project_with_doc)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    ref_a = await _make_ref(test_db, pid, host, "guard-a", chars=40_000, created_at=t0)
    ref_b = await _make_ref(test_db, pid, host, "guard-b", chars=40_000, created_at=t1)
    await build_consolidation_task(project_id=pid, target_doc_id=host, run_id=run_id)
    with pytest.raises(HTTPException) as exc:
        await next_reference(project_id=pid, run_id=run_id,
                             completed_reference_id=ref_b, completed_window_index=0)
    assert exc.value.status_code == 400
    # ref_a (oldest) is untouched by the refused stamp
    assert await _window_done(test_db, ref_a) in (None, 0)


@pytest.mark.asyncio
async def test_progress_reports_windows_alongside_references(project_with_doc, test_db):
    """Decision 5: progress reports windows so a long run does not read '0 of 1' while
    walking a multi-window reference. windows.total sums window_count over live refs;
    windows.done counts consumed windows (completed refs + the walked cursor)."""
    from datetime import datetime, timezone

    from memory._window import window_count
    from memory.task_builder import build_consolidation_task, next_reference

    pid, host, _uid, run_id = await _run(project_with_doc)
    ref = await _make_ref(test_db, pid, host, "prog", chars=40_000,
                          created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    full = await _raw_content(test_db, ref)
    total = window_count(len(full))

    portion = await build_consolidation_task(
        project_id=pid, target_doc_id=host, run_id=run_id,
    )
    win_progress = portion["progress"]["windows"]
    assert win_progress["total"] == total
    assert win_progress["done"] == 0
    assert portion["progress"]["total"] == 1  # one reference

    after = await next_reference(project_id=pid, run_id=run_id,
                                 completed_reference_id=ref, completed_window_index=0)
    assert after["progress"]["windows"]["done"] == 1


@pytest.mark.asyncio
async def test_progress_and_served_total_agree_on_padded_content(
    project_with_doc, test_db,
):
    """F1: the slicer serves RAW content (no strip), so `progress.windows.total`
    (computed from `string::len(content)`) and the served `reference.window.total` are
    the SAME number even when the material has large leading/trailing whitespace. The
    pre-fix code stripped in the slicer but not in `_progress`, so a padded reference
    here would report total=4 in progress and total=3 in the served window."""
    from datetime import datetime, timezone

    from memory._window import window_count

    from db import create_record

    pid, host, uid = project_with_doc
    body = _words(40_000)
    # 8 000 chars of padding (4k each side) — enough to cross a round() boundary:
    # raw ~48 000 → 4 windows; stripped ~40 000 → 3 windows.
    padded = ("\n" * 4_000) + body + ("\n" * 4_000)
    ref_id = "win-pad"
    await create_record("documents", ref_id, {
        "project_id": pid, "parent_id": host, "title": "padded",
        "content": padded, "path": f"_ref/{ref_id}.md", "is_index": False,
        "is_reference": True, "media_type": "markdown", "processing_status": "ready",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    })
    expected = window_count(len(padded))
    assert window_count(len(padded.strip())) != expected, (
        "test setup: padding must cross a window-count boundary"
    )

    from memory.run_key import mint_memory_run_key
    from memory.task_builder import build_consolidation_task

    run_id = (await mint_memory_run_key(user_id=uid, project_id=pid)).run_id
    portion = await build_consolidation_task(
        project_id=pid, target_doc_id=host, run_id=run_id,
    )
    assert portion["reference"]["window"]["total"] == expected
    assert portion["progress"]["windows"]["total"] == expected


@pytest.mark.asyncio
async def test_advance_window_is_atomic_compare_and_set(project_with_doc, test_db):
    """F2: two advances from the SAME expected window let only ONE land — the cursor
    moves by exactly one, never two (a concurrent pair of next_reference calls cannot
    double-advance or lose an update). `_advance_window` is the CAS; calling it directly
    simulates two concurrent callers that both read the cursor at 0."""
    from datetime import datetime, timezone

    from memory._portion import _advance_window
    from memory._window import window_count

    pid, host, _uid = project_with_doc
    ref = await _make_ref(test_db, pid, host, "cas", chars=40_000,
                          created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    full = await _raw_content(test_db, ref)
    n = window_count(len(full))
    assert n >= 2

    # both callers read the cursor at 0 and try to advance to 1
    first = await _advance_window(test_db, ref, expected_done=0, next_index=1)
    second = await _advance_window(test_db, ref, expected_done=0, next_index=1)
    assert (first, second) in {(True, False), (False, True)}, (
        f"exactly one CAS advance must land, got {(first, second)}"
    )
    assert await _window_done(test_db, ref) == 1, "cursor advanced by ONE, not two"

    # a genuine next advance (expecting 1) still lands — the CAS did not strand the walk
    third = await _advance_window(test_db, ref, expected_done=1, next_index=2)
    assert third is True
    assert await _window_done(test_db, ref) == 2


# ─── DB read helpers ─────────────────────────────────────────────────────────


async def _raw_row(test_db, ref_id) -> dict:
    rows = await test_db.query(
        "SELECT content, mem_consolidated_at, mem_consolidated_run, mem_window_done "
        "FROM type::record('documents', $id)",
        {"id": ref_id},
    )
    return (rows or [{}])[0]


async def _raw_content(test_db, ref_id) -> str:
    return (await _raw_row(test_db, ref_id)).get("content") or ""


async def _is_consumed(test_db, ref_id) -> bool:
    return (await _raw_row(test_db, ref_id)).get("mem_consolidated_at") is not None


async def _window_done(test_db, ref_id):
    return (await _raw_row(test_db, ref_id)).get("mem_window_done")


# silence unused-import linters for math (kept for future coverage math)
_ = math

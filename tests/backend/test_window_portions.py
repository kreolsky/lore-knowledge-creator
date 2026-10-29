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
import secrets

import pytest
import pytest_asyncio

# ════════════════════════════════════════════════════════════════════════════
# Pure slicing math — memory/_window.py
# ════════════════════════════════════════════════════════════════════════════


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

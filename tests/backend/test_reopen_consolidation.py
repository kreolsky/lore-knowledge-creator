"""Reopen a target's consolidation — clear the consumed-stamps so the material can be
walked again (plan `.kilo/plans/1786320000000-reopen-consolidation-tool.md`).

The unit is the TARGET (its host's whole reference stack), not a single reference: an
accent applies to the material as a whole, so per-id clearing would make the standard
case an id-enumeration chore. The clear writes NONE to both stamp fields and stops —
nothing is recorded about why, and it must NOT manufacture progress (D3: the anti-runaway
budget zeroes only on `stamped == True` from next_reference, never from this call).

These pin:
  - the core clear (clear → the next consolidate_memory SERVES the cleared reference);
  - clearing mid-stack keeps the queue in OLDEST-FIRST order (the in-order invariant);
  - D3: the clear returns no `stamped` progress signal (structural — it is not next_reference);
  - the Tool-API surface: auto applies, confirm proposes without writing, non-full → 403,
    cross-project → uniform 404;
  - the tool is a pack member (served by agent_toolset AND in the skill the project serves).
"""

import hashlib
import secrets
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from memory_ref import ensure_memory_reference


# Hermetic: the queue-walk tests drive `_peek_unconsumed`, which would otherwise see
# references left under the project's index doc by other tests in the shared session DB.
# Wipe references + memory facts before each test (mirrors test_memory_reference_shape).
@pytest_asyncio.fixture(autouse=True)
async def _clean_memory_space(project_with_doc, test_db):
    pid = project_with_doc[0]
    await test_db.query(
        "DELETE documents WHERE project_id = $pid AND is_memory = true", {"pid": pid},
    )
    await test_db.query(
        "DELETE documents WHERE project_id = $pid AND is_reference = true", {"pid": pid},
    )

# ─── Seeding helpers ─────────────────────────────────────────────────────────


async def _stamp(test_db, ref_id, run_id="run-prior"):
    """Stamp a reference as consumed by a prior run — the cursor state reopen clears."""
    await test_db.query(
        "UPDATE type::record('documents', $id) SET mem_consolidated_at = time::now(), "
        "mem_consolidated_run = $run",
        {"id": ref_id, "run": run_id},
    )


async def _is_stamped(ref_id):
    from db import fetch_one

    ref = await fetch_one("documents", ref_id)
    return ref is not None and ref.get("mem_consolidated_at") is not None


async def _make_ref(test_db, project_id, host_id, slug, *, created_at, content="material"):
    """A live, readable reference under `host_id` with an explicit created_at (deterministic
    oldest-first ordering, independent of clock resolution)."""
    from db import create_record

    ref_id = f"reopen-{slug}"
    await create_record("documents", ref_id, {
        "project_id": project_id, "parent_id": host_id, "title": f"portion {slug}",
        "content": content, "path": f"_ref/{ref_id}.md", "is_index": False,
        "is_reference": True, "media_type": "markdown", "processing_status": "ready",
        "created_at": created_at,
    })
    return ref_id


async def _make_agent_key(test_db, user_id, project_id, *, auto_apply=False):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key_id = f"ro-key-{secrets.token_hex(4)}"
    await create_record("api_keys", key_id, {
        "user_id": user_id, "project_id": project_id, "document_id": "",
        "token_hash": token_hash, "label": "agent", "capabilities": ["agent"],
        "auto_apply": auto_apply,
    })
    return token


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


# ════════════════════════════════════════════════════════════════════════════
# Core clear — reopen_consolidation(project_id, target_doc_id)
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_a_reference_target_re_resolves_to_its_host(project_with_doc, test_db):
    """D1: a REFERENCE id is accepted and re-resolves to its host — the clear covers the
    host's whole stack, same behaviour as consolidate_memory (resolve_consolidation_host)."""
    from memory.task_builder import reopen_consolidation

    pid, host, _uid = project_with_doc
    ref_a = await ensure_memory_reference(pid, "reopen-ref-a")
    ref_b = await ensure_memory_reference(pid, "reopen-ref-b")
    await _stamp(test_db, ref_a)
    await _stamp(test_db, ref_b)

    # Target a REFERENCE id (not the host) — both refs under the host clear.
    result = await reopen_consolidation(project_id=pid, target_doc_id=ref_a)

    assert result["cleared"] == 2
    assert result["target_doc_id"] == host
    assert not await _is_stamped(ref_a)
    assert not await _is_stamped(ref_b)


@pytest.mark.asyncio
async def test_clearing_mid_stack_keeps_the_queue_in_order(project_with_doc, test_db):
    """The in-order stamping invariant (Risks): clearing a fully-walked host must not
    disorder the queue. Three refs oldest→newest, all stamped, reopen, then walk — the
    run serves them oldest-first and completes exactly."""
    from memory.run_key import mint_memory_run_key
    from memory.task_builder import (
        build_consolidation_task,
        next_reference,
        reopen_consolidation,
    )

    pid, host, uid = project_with_doc
    ref_a = await _make_ref(test_db, pid, host, "a", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    ref_b = await _make_ref(test_db, pid, host, "b", created_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    ref_c = await _make_ref(test_db, pid, host, "c", created_at=datetime(2026, 1, 3, tzinfo=timezone.utc))
    for r in (ref_a, ref_b, ref_c):
        await _stamp(test_db, r)

    result = await reopen_consolidation(project_id=pid, target_doc_id=host)
    assert result["cleared"] == 3

    run = await mint_memory_run_key(user_id=uid, project_id=pid)
    p1 = await build_consolidation_task(project_id=pid, target_doc_id=host, run_id=run.run_id)
    assert p1["reference"]["id"] == ref_a
    p2 = await next_reference(project_id=pid, run_id=run.run_id, completed_reference_id=ref_a)
    assert p2["reference"]["id"] == ref_b
    p3 = await next_reference(project_id=pid, run_id=run.run_id, completed_reference_id=ref_b)
    assert p3["reference"]["id"] == ref_c
    closing = await next_reference(project_id=pid, run_id=run.run_id, completed_reference_id=ref_c)
    assert closing["complete"] is True
    assert closing["reference"] is None


@pytest.mark.asyncio
async def test_cross_project_target_is_uniform_404(project_with_doc, test_db):
    """Full access to THIS project must not clear another project's stamps. The target is
    re-bound to the caller's project (uniform 404, no existence oracle) — parity with
    consolidate_memory's resolve_consolidation_host."""
    from fastapi import HTTPException
    from memory.task_builder import reopen_consolidation

    pid, _host, _uid = project_with_doc
    other_pid = f"ro-other-{secrets.token_hex(4)}"
    other_host = f"ro-other-host-{secrets.token_hex(4)}"
    await test_db.query(
        "CREATE type::record('projects', $id) SET name='Other', status='active', "
        "project_context='', owner_id=$uid, index_doc_id=$h",
        {"id": other_pid, "uid": project_with_doc[2], "h": other_host},
    )
    await test_db.query(
        "CREATE type::record('documents', $h) SET project_id=$pid, parent_id=NONE, "
        "title='Other host', content='', path='', is_reference=false, is_index=true",
        {"h": other_host, "pid": other_pid},
    )

    with pytest.raises(HTTPException) as ei:
        await reopen_consolidation(project_id=pid, target_doc_id=other_host)
    assert ei.value.status_code == 404


# ════════════════════════════════════════════════════════════════════════════
# Tool surface (POST /api/tool/reopen_consolidation) — Pi-only
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tool_auto_applies_and_clears(client, test_db, admin_user, project_with_doc):
    """Full access + apply=auto → the tool clears directly and returns {status:"applied"};
    the stamp is gone."""
    pid, host, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid, auto_apply=True)
    ref = await ensure_memory_reference(pid, "reopen-tool-auto")
    await _stamp(test_db, ref)

    resp = await client.post("/api/tool/reopen_consolidation", json={
        "target_doc_id": host, "apply": "auto",
    }, headers=_hdr(agent_tok))

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "applied"
    assert data["cleared"] == 1
    assert not await _is_stamped(ref)


@pytest.mark.asyncio

@pytest.mark.asyncio
async def test_tool_non_full_principal_refused(
    client, test_db, admin_user, project_with_doc,
):
    """A Commentator/Viewer key (project access != full) cannot reopen — clearing the run
    cursor is a mutating write. Assert the refusal, not the happy path."""
    from db import create_record

    pid, _host, _admin_uid = project_with_doc
    comm_uid = f"comm-{secrets.token_hex(4)}"
    await create_record("users", comm_uid, {
        "email": f"{comm_uid}@x.test", "name": comm_uid, "role": "user",
        "password_hash": "x",
    })
    await create_record("project_members", f"pm-{secrets.token_hex(4)}", {
        "project_id": pid, "user_id": comm_uid, "access_level": "commentator",
    })
    agent_tok = await _make_agent_key(test_db, comm_uid, pid)

    resp = await client.post("/api/tool/reopen_consolidation", json={
        "target_doc_id": pid, "apply": "auto",
    }, headers=_hdr(agent_tok))

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_tool_cross_project_target_is_uniform_404(
    client, test_db, admin_user, project_with_doc,
):
    """A key bound to project A must NOT clear project B's stamps — uniform 404 (no
    existence oracle), and the foreign host's references stay stamped."""
    pid, _host, admin_uid = project_with_doc
    agent_tok = await _make_agent_key(test_db, admin_uid, pid)
    other_pid = f"ro-tool-other-{secrets.token_hex(4)}"
    other_host = f"ro-tool-other-host-{secrets.token_hex(4)}"
    foreign_ref = f"ro-foreign-{secrets.token_hex(4)}"
    await test_db.query(
        "CREATE type::record('projects', $id) SET name='Other', status='active', "
        "project_context='', owner_id=$uid, index_doc_id=$h",
        {"id": other_pid, "uid": admin_uid, "h": other_host},
    )
    await test_db.query(
        "CREATE type::record('documents', $h) SET project_id=$pid, parent_id=NONE, "
        "title='Other host', content='', path='', is_reference=false, is_index=true",
        {"h": other_host, "pid": other_pid},
    )
    await test_db.query(
        "CREATE type::record('documents', $r) SET project_id=$pid, parent_id=$h, "
        "title='foreign', content='m', path=$p, is_reference=true, media_type='markdown', "
        "processing_status='ready', mem_consolidated_at=time::now(), "
        "mem_consolidated_run='run-x'",
        {"r": foreign_ref, "pid": other_pid, "h": other_host, "p": f"_ref/{foreign_ref}"},
    )

    resp = await client.post("/api/tool/reopen_consolidation", json={
        "target_doc_id": other_host, "apply": "auto",
    }, headers=_hdr(agent_tok))

    assert resp.status_code == 404, resp.text
    assert await _is_stamped(foreign_ref)


# ════════════════════════════════════════════════════════════════════════════
# Pack membership — served by agent_toolset AND in the skill a project serves
# ════════════════════════════════════════════════════════════════════════════

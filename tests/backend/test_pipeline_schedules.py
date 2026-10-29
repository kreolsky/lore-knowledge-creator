"""S3 (plan 1786740208300) — project-scoped pipeline schedules.

# INVARIANT (claim-before-dispatch): a due schedule is CLAIMED by a conditional
# UPDATE (the proposal-claim primitive — CAS by predicate) BEFORE the enqueue, so
# two concurrent ticks dispatch it exactly ONCE. Why: the arq cron lock already
# dedups the tick per minute slot, but the claim guards the real hazards — a
# manual tick overlapping the cron tick, and any future sub-minute cadence; the
# plan's R3 is precisely "fires on every replica instead of once".

# INVARIANT (no wedge): a failing row records its error (last_status='error' +
# last_error) and the tick continues to the next row; the errored row's
# last_run_at still advanced at claim, so it becomes due again interval_s later —
# a failing schedule never blocks the tick or its own next run.
"""

import asyncio
import json

# No module-level asyncio mark: asyncio_mode=auto (tests/pytest.ini) covers the
# async tests, and this module also carries a SYNC test — a module mark would
# warn on it.


async def _seed_extract_param_docs(test_db, project_id: str) -> dict:
    """Create the real documents a valid extractor schedule points at, and return
    the params dict naming them (create + claim validation resolve every
    declared id-param inside the schedule's project)."""
    from db import create_record

    ids = {
        "reference_id": "psched-ref", "source_doc_id": "psched-src",
        "config_doc_id": "psched-cfg", "target_doc_id": "psched-tgt",
    }
    for key, doc_id in ids.items():
        await create_record("documents", doc_id, {
            "project_id": project_id, "parent_id": None,
            "title": key, "content": "", "path": f"{doc_id}.md",
            "is_index": False, "is_reference": False,
        })
    return {**ids, "project_id": project_id}


async def _seed_schedule(
    test_db, schedule_id, project_id, user_id, *,
    pipeline="extractor", enabled=True, interval_s=60,
    params=None, last_run_at=None,
):
    from db import create_record

    row = {
        "project_id": project_id,
        "pipeline": pipeline,
        "interval_s": interval_s,
        "enabled": enabled,
        "params_json": json.dumps(
            params if params is not None else {
                "reference_id": "psched-ref", "source_doc_id": "psched-src",
                "config_doc_id": "psched-cfg", "target_doc_id": "psched-tgt",
                "project_id": project_id,
            }
        ),
        "created_by": user_id,
    }
    if last_run_at is not None:
        row["last_run_at"] = last_run_at
    await create_record("pipeline_schedules", schedule_id, row)
    return schedule_id


class _EnqueueSpy:
    """Captures (function_name, kwargs, job_id); optionally slows each call to widen
    the claim race window in the two-tick test."""

    def __init__(self, delay: float = 0.0):
        self.calls: list[dict] = []
        self._delay = delay

    async def __call__(self, function_name, *args, job_id=None, queue=None, **kwargs):
        if self._delay:
            await asyncio.sleep(self._delay)
        self.calls.append({
            "function_name": function_name, "args": args,
            "kwargs": kwargs, "job_id": job_id, "queue": queue,
        })


# ─── REST CRUD ────────────────────────────────────────────────────────────────


async def test_cross_project_params_rejected_at_create(
    client, test_db, admin_user, project_with_doc,
):
    """SECURITY: a full-access member of project A must not schedule a run whose
    ids point into project B — the frozen params are the kwargs the worker runs
    with no user context (the one extractor entry point without per-call access
    checks). Testing the principal that must be REFUSED."""
    from db import create_record

    pid, _idx, _uid = project_with_doc
    valid = await _seed_extract_param_docs(test_db, pid)
    await create_record("documents", "psched-foreign-doc", {
        "project_id": "other-project", "parent_id": None, "title": "Foreign",
        "content": "", "path": "foreign.md", "is_index": False,
        "is_reference": False,
    })
    base = {
        "project_id": pid, "pipeline": "extractor", "interval_s": 3600,
    }
    cookie = {"lore_session": admin_user[1]}

    # A foreign document id in any declared slot is refused (the error names it).
    for key in ("reference_id", "source_doc_id", "config_doc_id", "target_doc_id"):
        params = dict(valid)
        params[key] = "psched-foreign-doc"
        resp = await client.post(
            "/api/pipeline-schedules", json={**base, "params": params},
            cookies=cookie,
        )
        assert resp.status_code == 400, (key, resp.text)
        assert key in resp.json()["detail"]

    # params['project_id'] must be the schedule's own project.
    resp = await client.post(
        "/api/pipeline-schedules",
        json={**base, "params": {
            "reference_id": "r", "source_doc_id": "s", "config_doc_id": "c",
            "target_doc_id": "t", "project_id": "other-project",
        }},
        cookies=cookie,
    )
    assert resp.status_code == 400, resp.text

    # A nonexistent document id is refused too (no silent creation-time pass to a
    # row that would error every tick).
    resp = await client.post(
        "/api/pipeline-schedules",
        json={**base, "params": {
            "reference_id": "does-not-exist", "source_doc_id": "s",
            "config_doc_id": "c", "target_doc_id": "t", "project_id": pid,
        }},
        cookies=cookie,
    )
    assert resp.status_code == 400, resp.text


async def test_signature_mismatched_params_rejected_at_create(
    client, test_db, admin_user, project_with_doc,
):
    """A params key the task signature does not accept is a 400 at create (the
    error names the key), not a schedule that enqueues 'ok' and then TypeErrors
    forever with last_status stuck at 'running'-after-'ok'."""
    pid, _idx, _uid = project_with_doc
    resp = await client.post(
        "/api/pipeline-schedules",
        json={
            "project_id": pid, "pipeline": "extractor", "interval_s": 3600,
            "params": {
                "referenceId": "camelCase", "source_doc_id": "s",
                "config_doc_id": "c", "target_doc_id": "t", "project_id": pid,
            },
        },
        cookies={"lore_session": admin_user[1]},
    )
    assert resp.status_code == 400, resp.text
    assert "signature" in resp.json()["detail"]


async def test_non_member_cannot_create(
    client, test_db, admin_user, regular_user, project_with_doc,
):
    pid, _idx, _uid = project_with_doc
    # Uniform 404 (no existence oracle) — the project's convention for a
    # non-member probing a project they cannot see.
    resp = await client.post(
        "/api/pipeline-schedules",
        json={
            "project_id": pid, "pipeline": "extractor", "interval_s": 3600,
            "params": {"x": 1},
        },
        cookies={"lore_session": regular_user[1]},
    )
    assert resp.status_code == 404, resp.text


async def test_patch_pauses_and_delete_soft_deletes(
    client, test_db, admin_user, project_with_doc,
):
    pid, _idx, _uid = project_with_doc
    params = await _seed_extract_param_docs(test_db, pid)
    created = (await client.post(
        "/api/pipeline-schedules",
        json={
            "project_id": pid, "pipeline": "extractor", "interval_s": 3600,
            "params": params,
        },
        cookies={"lore_session": admin_user[1]},
    )).json()
    sid = created["schedule_id"]

    paused = await client.patch(
        f"/api/pipeline-schedules/{sid}", json={"enabled": False},
        cookies={"lore_session": admin_user[1]},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["enabled"] is False

    deleted = await client.delete(
        f"/api/pipeline-schedules/{sid}",
        cookies={"lore_session": admin_user[1]},
    )
    assert deleted.status_code == 200, deleted.text
    listed = await client.get(
        "/api/pipeline-schedules", params={"project_id": pid},
        cookies={"lore_session": admin_user[1]},
    )
    assert listed.json() == []


async def test_project_delete_stops_its_schedules(
    client, test_db, admin_user, project_with_doc, monkeypatch,
):
    """Deleting a project stops its schedules: delete_project soft-deletes the
    project's pipeline_schedules rows. Why: the tick is the one ACTIVE consumer
    among project children (api_keys/agent_configs are passive — gated by access
    checks on read); the due predicate checks only the schedule row, so this
    cascade is the single thing between a deleted project and eternal dispatches.
    Driven through the ROUTE (not a hand-soft-deleted project row) — the cascade
    is the fix under test."""
    from jobs import tasks

    pid, _idx, uid = project_with_doc
    await _seed_schedule(test_db, "ps-dead", pid, uid)

    resp = await client.delete(
        f"/api/projects/{pid}", cookies={"lore_session": admin_user[1]},
    )
    assert resp.status_code == 200, resp.text

    # fetch_one None-checks soft-deleted rows, so assert the tombstone directly.
    from db import get_db

    rows = await (await get_db()).query(
        "SELECT deleted_at FROM type::record('pipeline_schedules', $id)",
        {"id": "ps-dead"},
    )
    assert rows and rows[0].get("deleted_at") is not None

    spy = _EnqueueSpy()
    monkeypatch.setattr("jobs.pool.enqueue", spy)
    await tasks.pipeline_schedule_tick_task({"redis": None})
    assert spy.calls == []


# ─── the tick ─────────────────────────────────────────────────────────────────


async def test_two_concurrent_ticks_claim_exactly_once(
    client, test_db, admin_user, project_with_doc, monkeypatch,
):
    """R3: the singleton property IS the point — two ticks racing on the same due
    row dispatch exactly once (the conditional-UPDATE claim, not the arq cron
    lock, is what this exercises)."""
    from jobs import tasks

    pid, _idx, uid = project_with_doc
    await _seed_extract_param_docs(test_db, pid)
    await _seed_schedule(test_db, "ps-race", pid, uid)

    spy = _EnqueueSpy(delay=0.05)
    monkeypatch.setattr("jobs.pool.enqueue", spy)
    await asyncio.gather(
        tasks.pipeline_schedule_tick_task({"redis": None}),
        tasks.pipeline_schedule_tick_task({"redis": None}),
    )
    assert len(spy.calls) == 1


async def test_tick_refuses_cross_project_params(
    client, test_db, admin_user, project_with_doc, monkeypatch,
):
    """SECURITY (defense in depth behind create validation): a row whose frozen
    params point at another project's documents (hand-edited, or written by a
    deploy window without create validation) is REFUSED at claim — no dispatch,
    error recorded. Testing the principal that must be REFUSED."""
    from db import create_record
    from jobs import tasks

    pid, _idx, uid = project_with_doc
    await create_record("documents", "psched-foreign-doc", {
        "project_id": "other-project", "parent_id": None, "title": "Foreign",
        "content": "", "path": "foreign.md", "is_index": False,
        "is_reference": False,
    })
    await _seed_schedule(test_db, "ps-cross", pid, uid, params={
        "reference_id": "psched-foreign-doc", "source_doc_id": "psched-src",
        "config_doc_id": "psched-cfg", "target_doc_id": "psched-tgt",
        "project_id": "other-project",
    })

    spy = _EnqueueSpy()
    monkeypatch.setattr("jobs.pool.enqueue", spy)
    await tasks.pipeline_schedule_tick_task({"redis": None})
    assert spy.calls == []

    from db import fetch_one

    row = await fetch_one("pipeline_schedules", "ps-cross")
    assert row["last_status"] == "error"
    assert "project" in (row.get("last_error") or "")

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


async def test_create_and_list_schedule_roundtrip(
    client, test_db, admin_user, project_with_doc,
):
    pid, _idx, uid = project_with_doc
    _tok_admin = admin_user[1]
    params = await _seed_extract_param_docs(test_db, pid)
    resp = await client.post(
        "/api/pipeline-schedules",
        json={
            "project_id": pid, "pipeline": "extractor", "interval_s": 3600,
            "params": params,
        },
        cookies={"lore_session": _tok_admin},
    )
    assert resp.status_code == 200, resp.text
    created = resp.json()
    assert created["pipeline"] == "extractor"
    assert created["interval_s"] == 3600
    assert created["enabled"] is True
    # The frozen params round-trip verbatim (the tick dispatches them as kwargs).
    assert created["params"] == params

    listed = await client.get(
        "/api/pipeline-schedules", params={"project_id": pid},
        cookies={"lore_session": _tok_admin},
    )
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert len(rows) == 1
    assert rows[0]["schedule_id"] == created["schedule_id"]


async def test_unknown_pipeline_rejected_at_create(
    client, admin_user, project_with_doc,
):
    pid, _idx, _uid = project_with_doc
    resp = await client.post(
        "/api/pipeline-schedules",
        json={
            "project_id": pid, "pipeline": "not-a-pipeline", "interval_s": 3600,
            "params": {"x": 1},
        },
        cookies={"lore_session": admin_user[1]},
    )
    # The registry is the derived source — the error names the known pipelines.
    assert resp.status_code == 400, resp.text
    assert "extractor" in resp.json()["detail"]


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


async def test_tick_dispatches_due_schedule_once(
    client, test_db, admin_user, project_with_doc, monkeypatch,
):
    from jobs import tasks

    pid, _idx, uid = project_with_doc
    await _seed_extract_param_docs(test_db, pid)
    await _seed_schedule(test_db, "ps-1", pid, uid)

    spy = _EnqueueSpy()
    monkeypatch.setattr("jobs.pool.enqueue", spy)
    await tasks.pipeline_schedule_tick_task({"redis": None})

    assert len(spy.calls) == 1
    call = spy.calls[0]
    # Dispatch addresses the registry row's TASK (extract_task), with the frozen
    # params as kwargs and a per-claim job_id (enqueue-level idempotency).
    assert call["function_name"] == "extract_task"
    assert call["kwargs"]["reference_id"] == "psched-ref"
    assert call["job_id"] and call["job_id"].startswith("psched:ps-1:")

    from db import fetch_one

    row = await fetch_one("pipeline_schedules", "ps-1")
    assert row["last_status"] == "ok"
    assert row["last_run_at"] is not None

    # Not due again — the claim advanced last_run_at by the full interval.
    await tasks.pipeline_schedule_tick_task({"redis": None})
    assert len(spy.calls) == 1


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


async def test_tick_limit_50_carries_over_to_the_next_tick(
    client, test_db, admin_user, project_with_doc, monkeypatch,
):
    """The tick's due-scan is bounded (LIMIT 50 — memory discipline per tick). A
    backlog larger than the bound must CARRY OVER, not be dropped: the next tick
    picks up the remainder, and every schedule dispatches exactly once overall
    (the claim prevents a re-scan from double-firing the first 50)."""
    from jobs import tasks

    pid, _idx, uid = project_with_doc
    await _seed_extract_param_docs(test_db, pid)
    for i in range(55):
        await _seed_schedule(test_db, f"ps-carry-{i:02d}", pid, uid)

    spy = _EnqueueSpy()
    monkeypatch.setattr("jobs.pool.enqueue", spy)

    await tasks.pipeline_schedule_tick_task({"redis": None})
    assert len(spy.calls) == 50, "first tick must be bounded by the LIMIT"

    await tasks.pipeline_schedule_tick_task({"redis": None})
    assert len(spy.calls) == 55, "second tick must dispatch the carried-over remainder"

    await tasks.pipeline_schedule_tick_task({"redis": None})
    assert len(spy.calls) == 55, "a third tick must find nothing due (no double fire)"


async def test_paused_schedule_does_not_fire(
    client, test_db, admin_user, project_with_doc, monkeypatch,
):
    from jobs import tasks

    pid, _idx, uid = project_with_doc
    await _seed_schedule(test_db, "ps-off", pid, uid, enabled=False)

    spy = _EnqueueSpy()
    monkeypatch.setattr("jobs.pool.enqueue", spy)
    await tasks.pipeline_schedule_tick_task({"redis": None})
    assert spy.calls == []


async def test_recently_run_schedule_not_due(
    client, test_db, admin_user, project_with_doc, monkeypatch,
):
    from datetime import datetime, timezone

    from jobs import tasks

    pid, _idx, uid = project_with_doc
    await _seed_schedule(
        test_db, "ps-fresh", pid, uid,
        last_run_at=datetime.now(timezone.utc),
    )

    spy = _EnqueueSpy()
    monkeypatch.setattr("jobs.pool.enqueue", spy)
    await tasks.pipeline_schedule_tick_task({"redis": None})
    assert spy.calls == []


async def test_failing_row_is_recorded_and_does_not_wedge(
    client, test_db, admin_user, project_with_doc, monkeypatch,
):
    """A row whose pipeline is unknown (e.g. the registry dropped it) records the
    error and — critically — the SAME tick still dispatches the healthy sibling;
    the errored row is attempted again once due."""
    from jobs import tasks

    pid, _idx, uid = project_with_doc
    await _seed_extract_param_docs(test_db, pid)
    await _seed_schedule(test_db, "ps-bad", pid, uid, pipeline="removed-pipeline")
    await _seed_schedule(test_db, "ps-good", pid, uid)

    spy = _EnqueueSpy()
    monkeypatch.setattr("jobs.pool.enqueue", spy)
    await tasks.pipeline_schedule_tick_task({"redis": None})

    # The healthy sibling dispatched despite the bad sibling failing first.
    assert [c["function_name"] for c in spy.calls] == ["extract_task"]

    from db import fetch_one

    bad = await fetch_one("pipeline_schedules", "ps-bad")
    assert bad["last_status"] == "error"
    assert "removed-pipeline" in (bad.get("last_error") or "")

    # Force the bad row due again — the tick retries it (no wedge).
    await test_db.query(
        "UPDATE type::record('pipeline_schedules', $id) SET last_run_at = NONE",
        {"id": "ps-bad"},
    )
    await tasks.pipeline_schedule_tick_task({"redis": None})
    bad_after = await fetch_one("pipeline_schedules", "ps-bad")
    assert bad_after["last_run_at"] is not None  # claimed again
    assert bad_after["last_status"] == "error"


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


async def test_tick_rebinds_project_id_from_the_row(
    client, test_db, admin_user, project_with_doc, monkeypatch,
):
    """params['project_id'] is FORCE-overridden from the schedule row at claim —
    the row is the authority, a stale/lying params value never reaches the task."""
    from jobs import tasks

    pid, _idx, uid = project_with_doc
    await _seed_extract_param_docs(test_db, pid)
    await _seed_schedule(test_db, "ps-rebind", pid, uid, params={
        "reference_id": "psched-ref", "source_doc_id": "psched-src",
        "config_doc_id": "psched-cfg", "target_doc_id": "psched-tgt",
        "project_id": "stale-lie",
    })

    spy = _EnqueueSpy()
    monkeypatch.setattr("jobs.pool.enqueue", spy)
    await tasks.pipeline_schedule_tick_task({"redis": None})
    assert len(spy.calls) == 1
    assert spy.calls[0]["kwargs"]["project_id"] == pid


async def test_tick_registered_as_function_and_cron(
    client, test_db, admin_user, project_with_doc,
):
    """The tick must be a registered function AND a cron entry — arq silently drops
    an unregistered one (the COMFY lesson), and a non-cron tick never fires."""
    from jobs.worker import WorkerSettings

    names = {getattr(f, "name", None) for f in WorkerSettings.functions}
    assert "pipeline_schedule_tick_task" in names
    cron_names = {c.coroutine.__name__ for c in WorkerSettings.cron_jobs}
    assert "pipeline_schedule_tick_task" in cron_names


def test_every_cron_spec_computes_a_next_run():
    """PIN (boot-crash class): an invalid cron spec (e.g. minute='*') raises inside
    arq's run_cron AT BOOT and kills the cron loop for ALL jobs — including the
    four maintenance crons. Every cron entry must compute a next_run."""
    from datetime import datetime, timezone

    from jobs.worker import WorkerSettings

    now = datetime.now(timezone.utc)
    for job in WorkerSettings.cron_jobs:
        job.calculate_next(now)
        assert job.next_run > now, (
            f"cron {job.coroutine.__name__} computed no next_run — an invalid "
            f"spec would crash the worker's cron loop at boot"
        )

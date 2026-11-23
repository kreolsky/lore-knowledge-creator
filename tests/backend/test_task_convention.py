"""The task loop (SYSTEM: task-convention): the pure selection edges (ordering, blocked dependencies, stale
leases, invalid reports), the schema refusals, the claim outcomes on LIVE
text, the concurrent-claim race, the MCP reach (init's shipped_skills, the
read-only key) and the refused principals (disabled skill, agent_name
injection).
"""
import asyncio
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from db import fetch_one

SCHEMA_HEADER = (
    "---\ntype: schema\nschema_for: task\nstatuses: [todo, doing, review, done]\n"
    "ready: todo\nclaimed: doing\nclosed: [done]\ninitial: todo\n"
    "priority: [urgent, normal]\n---\n\nThe log heading is `## Лог`.\n"
)

NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)


def _row(doc_id, title, props=None, error=None, parent=None, created=None):
    return {
        "id": doc_id, "title": title, "parent_id": parent,
        "props": props, "props_error": error, "created_at": created or 0,
    }


def _task(doc_id, title, status=None, **props):
    p = {"type": "task"}
    if status is not None:
        p["status"] = status
    p.update(props)
    return _row(doc_id, title, p)


def _schema_rows():
    return [_row("s1", "Task schema", {
        "type": "schema", "schema_for": "task",
        "statuses": ["todo", "doing", "review", "done"],
        "ready": "todo", "claimed": "doing", "closed": ["done"],
        "priority": ["urgent", "normal"],
    })]


# ─── Pure edges: selection, schema resolve, the header write ──────────────────


# select_next_task / resolve_task_schema / build_claimed_block are pure
# functions over (rows, schema, now) / rows / the block; each edge here is
# decided by exactly one branch of one of them, with no DB in the way.
# TEST-WHY: pure-logic-edge — the convention table, one branch per edge.
def test_convention_pure_edges():
    import pytest
    from fastapi import HTTPException

    from task_convention import (
        HeaderLineError,
        build_claimed_block,
        resolve_task_schema,
        select_next_task,
        splice_header_keys,
    )

    schema = resolve_task_schema(_schema_rows())

    # Selection: priority order first (urgent before normal, missing priority
    # last), then created_at (rows' own order — the sort is stable). The
    # stale claim is offered but behind the ready tasks of known priority; an
    # active claim is listed in in_progress, a ready task assigned to someone
    # else in reserved; a closed task and a non-task are neither offered nor
    # reported; a broken header and unknown/missing status are REPORTED.
    rows = _schema_rows() + [
        _task("t-normal", "Normal task", "todo"),
        _task("t-urgent", "Urgent task", "todo", priority="urgent"),
        _task("t-blocked", "Blocked task", "todo",
              depends=['"[Fix the importer](doc:t-dep)"']),
        _task("t-dep", "The dependency", "doing"),
        _task("t-free", "Free dependency", "done"),
        _task("t-after-free", "Unblocked by done dep", "todo",
              depends=["t-free"]),
        _task("t-stale", "Stale claim", "doing", assignee="other",
              claimed_until=(NOW - timedelta(hours=1)).isoformat()),
        _task("t-active", "Active claim", "doing", assignee="other",
              claimed_until=(NOW + timedelta(hours=1)).isoformat()),
        _task("t-reserved", "Reserved", "todo", priority="urgent",
              assignee="human"),
        _task("t-unknown", "Unknown status", "wip"),
        _task("t-nostatus", "No status"),
        _row("t-broken", "Broken header", error="malformed header YAML: boom"),
        _task("t-story", "Not a task", "todo", **{"type": "story"}),
    ]
    out = select_next_task(rows, schema, now=NOW)
    assert out["task"]["id"] == "t-urgent"
    assert "stale" not in out["task"]
    assert out["schema_id"] == "s1" and out["initial"] == "todo"
    assert out["counts"] == {
        "available": 4, "blocked": 1, "invalid": 3, "reserved": 1,
        "in_progress": 2, "stale": 1,
    }
    assert out["reserved"] == [
        {"id": "t-reserved", "title": "Reserved", "assignee": "human"},
    ]
    # t-dep: claimed status with no claimed_until reads as a live claim.
    assert [e["id"] for e in out["in_progress"]] == ["t-dep", "t-active"]
    assert out["in_progress"][1]["assignee"] == "other"
    # The named agent gets its own reserved task back into the offer pool.
    mine = select_next_task(rows, schema, now=NOW, agent_name="human")
    assert mine["reserved"] == [] and mine["counts"]["available"] == 5
    assert {e["id"] for e in out["invalid"]} == {
        "t-unknown", "t-nostatus", "t-broken",
    }
    assert out["blocked"] == [{
        "id": "t-blocked", "title": "Blocked task",
        "reason": "dependency not closed: t-dep (status 'doing')",
    }]

    # A dep whose status IS closed unblocks; an unresolvable entry and an
    # invisible target block with their own reasons.
    out2 = select_next_task(_schema_rows() + [
        _task("t-a", "A", "todo", depends=["nowhere-to-be-found"]),
        _task("t-b", "B", "todo", depends=["???"]),
        _task("t-c", "C", "todo", depends=["t-free"]),
        _task("t-free", "Free", "done"),
    ], schema, now=NOW)
    assert out2["task"]["id"] == "t-c"
    reasons = {b["id"]: b["reason"] for b in out2["blocked"]}
    assert "not visible" in reasons["t-a"]
    assert "unresolvable" in reasons["t-b"]

    # A malformed depends (a mapping, not a string/list) is REPORTED as
    # invalid — the dependency is never silently dropped to offer the task.
    out_dep = select_next_task(_schema_rows() + [
        _task("t-d", "D", "todo", **{"depends": {"doc": "x"}}),
    ], schema, now=NOW)
    assert out_dep["task"] is None and out_dep["counts"]["invalid"] == 1
    assert "depends must be" in out_dep["invalid"][0]["reason"]
    # A list/mapping priority is REPORTED too — it would otherwise fail the
    # sort and take next_task down for every task in the project.
    out_prio = select_next_task(_schema_rows() + [
        _task("t-p", "P", "todo", priority=["urgent"]),
        _task("t-q", "Q", "todo"),
    ], schema, now=NOW)
    assert out_prio["task"]["id"] == "t-q"
    assert out_prio["invalid"][0]["reason"] == "priority must be a single value"
    # A priority the schema list does not name is REPORTED, not sorted last —
    # a typo must not silently demote urgent work.
    out_typo = select_next_task(_schema_rows() + [
        _task("t-typo", "Typo", "todo", priority="hgih"),
        _task("t-none", "None", "todo"),
    ], schema, now=NOW)
    assert out_typo["task"]["id"] == "t-none"
    assert out_typo["invalid"] == [{
        "id": "t-typo", "title": "Typo", "reason": "unknown priority 'hgih'",
    }]
    # Dependencies resolve against `lookup` (everything the key sees), not
    # only the candidates `under` narrowed to.
    out_lookup = select_next_task(
        [_task("t-in", "In", "todo", depends=["t-out"])], schema, now=NOW,
        lookup=_schema_rows() + [_task("t-out", "Out", "done")],
    )
    assert out_lookup["task"]["id"] == "t-in"

    # Nothing available → task null; a lone stale claim wins, flagged.
    out3 = select_next_task(
        _schema_rows() + [_task("t-x", "X", "done")], schema, now=NOW,
    )
    assert out3["task"] is None and out3["counts"]["available"] == 0
    out4 = select_next_task(_schema_rows() + [
        _task("t-stale", "Stale", "doing", assignee="other",
              claimed_until=(NOW - timedelta(minutes=1)).isoformat()),
    ], schema, now=NOW)
    assert out4["task"]["id"] == "t-stale" and out4["task"]["stale"] is True

    # Schema resolve: every refusal names the project state that is wrong,
    # never a silent pick.
    with pytest.raises(HTTPException) as no_schema:
        resolve_task_schema([])
    assert no_schema.value.status_code == 409
    assert no_schema.value.detail["code"] == "no_schema"
    assert "type: schema" in no_schema.value.detail["detail"]
    with pytest.raises(HTTPException) as ambiguous:
        resolve_task_schema(_schema_rows() + _schema_rows())
    assert ambiguous.value.detail["code"] == "ambiguous_schema"
    assert "s1" in ambiguous.value.detail["detail"]

    def _broken(**override):
        props = {
            "type": "schema", "schema_for": "task",
            "statuses": ["todo", "doing", "done"],
            "ready": "todo", "claimed": "doing", "closed": ["done"],
        }
        props.update(override)
        return [_row("sx", "S", props)]

    for override, field in (
        ({"claimed": "wip"}, "claimed"),
        ({"ready": None}, "ready"),
        ({"initial": "wip"}, "initial"),
        ({"closed": ["gone"]}, "closed"),
        ({"statuses": []}, "statuses"),
        ({"priority": "urgent"}, "priority"),
    ):
        with pytest.raises(HTTPException) as invalid:
            resolve_task_schema(_broken(**override))
        assert invalid.value.detail["code"] == "invalid_schema", override
        assert field in invalid.value.detail["detail"], override
    ok = resolve_task_schema(_schema_rows())
    assert ok["initial"] == "todo" and ok["schema_id"] == "s1"
    assert ok["closed"] == ["done"] and ok["priority"] == ["urgent", "normal"]
    assert ok["archive_id"] is None
    assert resolve_task_schema(
        _broken(archive="[DONE](doc:arch1)"),
    )["archive_id"] == "arch1"
    with pytest.raises(HTTPException) as bad_archive:
        resolve_task_schema(_broken(archive="not a link!"))
    assert bad_archive.value.detail["code"] == "invalid_schema"

    # The header write: replace status, upsert the two lease keys (always
    # double-quoted), insert before the closing fence, keep the block's
    # trailing shape, and REFUSE a non-scalar under a claim key.
    out = build_claimed_block(
        "type: task\nstatus: todo\npriority: urgent\n",
        status="doing", agent_name="bot.one@example",
        claimed_until="2026-11-22T16:00:00+00:00",
    )
    assert out == (
        'type: task\nstatus: doing\npriority: urgent\n'
        'assignee: "bot.one@example"\n'
        'claimed_until: "2026-11-22T16:00:00+00:00"\n'
    )
    # A non-Latin name is written verbatim, not \u-escaped.
    out = build_claimed_block(
        'type: task\nstatus: doing\nassignee: "old"\n'
        'claimed_until: "2026-11-22T10:00:00+00:00"\n',
        status="doing", agent_name="Ола",
        claimed_until="2026-11-22T20:00:00+00:00",
    )
    assert out.count("assignee:") == 1 and 'assignee: "Ола"' in out
    assert out.count("claimed_until:") == 1
    assert '"2026-11-22T20:00:00+00:00"' in out
    assert build_claimed_block(
        "type: task\nstatus: todo",
        status="doing", agent_name="a",
        claimed_until="2026-11-22T16:00:00+00:00",
    ) == (
        'type: task\nstatus: doing\nassignee: "a"\n'
        'claimed_until: "2026-11-22T16:00:00+00:00"'
    )
    # An indented (nested-mapping) key is NOT the convention's key: it stays,
    # and the real top-level key is inserted.
    out = build_claimed_block(
        "type: task\nstatus: todo\nmeta:\n  status: nested\n",
        status="doing", agent_name="a",
        claimed_until="2026-11-22T16:00:00+00:00",
    )
    assert "  status: nested" in out and "\nstatus: doing" in out
    for block in (
        "type: task\nstatus: [todo, doing]\n",
        "type: task\nstatus: todo\nassignee: {name: x}\n",
        "type: task\nstatus: todo\nclaimed_until: |\n  soon\n",
    ):
        with pytest.raises(HeaderLineError):
            build_claimed_block(
                block, status="doing", agent_name="a",
                claimed_until="2026-11-22T16:00:00+00:00",
            )
    # The status transition's splice: replace status, DROP claimed_until,
    # keep assignee and every other line.
    assert splice_header_keys(
        'type: task\nstatus: doing\nassignee: "a"\n'
        'claimed_until: "2026-11-22T16:00:00+00:00"\npriority: urgent\n',
        {"status": "review"}, ("claimed_until",),
    ) == 'type: task\nstatus: review\nassignee: "a"\npriority: urgent\n'
    # A dropped key that is absent is a no-op; a non-scalar under it refuses.
    assert splice_header_keys(
        "type: task\nstatus: review\n", {"status": "done"}, ("claimed_until",),
    ) == "type: task\nstatus: done\n"
    with pytest.raises(HeaderLineError):
        splice_header_keys("type: task\nstatus: doing\nclaimed_until: [x]\n",
                           {"status": "review"}, ("claimed_until",))
    # A touched key whose value spans lines would leave its continuation
    # behind after the one-line replace — the re-parse check refuses it.
    from doc_properties import parse_header
    from task_convention import _splice_or_refuse
    claim = {"status": "doing", "assignee": '"a"', "claimed_until": '"x"'}
    for block in (
        "type: task\nstatus: todo\nassignee:\n  - someone\n",
        "type: task\nstatus: todo\nassignee: some\n  one\n",
    ):
        props, _ = parse_header(f"---\n{block}---\n")
        expected = {**props, "status": "doing", "assignee": "a",
                    "claimed_until": "x"}
        with pytest.raises(HTTPException) as spans:
            _splice_or_refuse(block, claim, (), expected)
        assert spans.value.detail["code"] == "non_scalar_claim_key"
    good = "type: task\nstatus: todo\n"
    props, _ = parse_header(f"---\n{good}---\n")
    assert "assignee: \"a\"" in _splice_or_refuse(good, claim, (), {
        **props, "status": "doing", "assignee": "a", "claimed_until": "x",
    })


# ─── Integration: the loop over the real stack ────────────────────────────────


async def _doc(client, token, pid, title, content, parent_id=None, **extra) -> str:
    resp = await client.post(
        "/api/documents",
        json={"project_id": pid, "title": title, "content": content,
              "parent_id": parent_id, **extra},
        cookies={"lore_session": token},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["document_id"]


async def _patch_content(client, token, doc_id, content) -> None:
    resp = await client.patch(
        f"/api/documents/{doc_id}", json={"content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text


async def _key(uid: str, pid: str, **extra) -> dict:
    from agent.keys import create_agent_key_row

    token, _, _ = await create_agent_key_row(uid, pid, **extra)
    return {"Authorization": f"Bearer {token}"}


async def _tool(client, name: str, headers: dict, **body):
    return await client.post(f"/api/tool/{name}", json=body, headers=headers)


async def _settle() -> None:
    """Let the fire-and-forget bus tasks (the projection subscriber) run."""
    await asyncio.sleep(0.3)


async def _seed_project(client, test_db, collab_project):
    """A schema doc + the task shapes the loop tests read."""
    pid, _shared, admin_token, _user_token, admin_uid, _user_uid = collab_project
    schema_id = await _doc(client, admin_token, pid, "Task schema", SCHEMA_HEADER)
    dep = await _doc(client, admin_token, pid, "Dependency", (
        "---\ntype: task\nstatus: doing\n---\n"
    ))
    blocked = await _doc(client, admin_token, pid, "Blocked task", (
        f"---\ntype: task\nstatus: todo\ndepends:\n  - \"[Dependency](doc:{dep})\"\n---\n"
    ))
    ready = await _doc(client, admin_token, pid, "Ready task", (
        "---\ntype: task\nstatus: todo\n---\n\nFix the thing.\n"
    ))
    await _settle()
    key = await _key(admin_uid, pid)
    return pid, admin_token, admin_uid, key, schema_id, {
        "dep": dep, "blocked": blocked, "ready": ready,
    }


async def _parse_row_header(doc_id):
    from doc_properties import parse_header

    row = await fetch_one("documents", doc_id)
    return parse_header(row.get("content"))


# The loop reads `props` (which lags the ~1s flush + the projection) but
# CLAIMS on the live text — the offer/claim skew, the outcome table and the
# two-caller race are the contract this pins, against the real subscriber,
# the real splice and the real edit_lock.
# TEST-WHY: cross-process-contract — REST/Tool-API writes, row + live reads.
async def test_claim_task_outcomes_and_race(client, test_db, collab_project):
    pid, admin_token, _uid, key, schema_id, ids = await _seed_project(
        client, test_db, collab_project,
    )

    # next_task: the ready task is offered, the dependency-blocked one is
    # reported, initial + schema_id ride along.
    resp = await _tool(client, "next_task", key)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task"]["id"] == ids["ready"]
    assert body["schema_id"] == schema_id and body["initial"] == "todo"
    assert body["counts"] == {
        "available": 1, "blocked": 1, "invalid": 0, "reserved": 0,
        "in_progress": 1, "stale": 0,
    }
    assert body["blocked"][0]["id"] == ids["blocked"]
    assert "not closed" in body["blocked"][0]["reason"]

    # claim → outcome claimed; the header in the ROW carries the splice
    # (status replaced, both lease keys double-quoted), and a pre-edit
    # checkpoint exists (the claim is reversible in History).
    resp = await _tool(client, "claim_task", key,
                       document_id=ids["ready"], agent_name="agent-a",
                       apply="auto")
    assert resp.status_code == 200, resp.text
    claimed = resp.json()
    assert claimed["outcome"] == "claimed" and claimed["assignee"] == "agent-a"
    props, err = await _parse_row_header(ids["ready"])
    assert err is None
    assert props["status"] == "doing"
    assert props["assignee"] == "agent-a"
    assert props["claimed_until"] == claimed["claimed_until"]
    checkpoints = await test_db.query(
        "SELECT label FROM checkpoints WHERE document_id = $id "
        "ORDER BY created_at DESC LIMIT 1",
        {"id": ids["ready"]},
    )
    assert checkpoints and checkpoints[0]["label"] == "agent-auto"
    # The claim reached the projection through the same subscriber.
    await _settle()
    row = await fetch_one("documents", ids["ready"])
    assert row["props"]["assignee"] == "agent-a"

    # Same agent → renewed (the lease extends).
    resp = await _tool(client, "claim_task", key,
                       document_id=ids["ready"], agent_name="agent-a",
                       apply="auto")
    renewed = resp.json()
    assert renewed["outcome"] == "renewed"
    assert renewed["claimed_until"] > claimed["claimed_until"]

    # Another agent while the lease lives → 409 already_claimed naming them.
    resp = await _tool(client, "claim_task", key,
                       document_id=ids["ready"], agent_name="agent-b",
                       apply="auto")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "already_claimed"
    assert detail["assignee"] == "agent-a"
    assert detail["claimed_until"] == renewed["claimed_until"]

    # An expired lease written by hand → offered stale by next_task, and the
    # claim is a takeover.
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await _patch_content(client, admin_token, ids["ready"], (
        f"---\ntype: task\nstatus: doing\nassignee: \"agent-a\"\n"
        f"claimed_until: \"{past}\"\n---\n\nFix the thing.\n"
    ))
    await _settle()
    resp = await _tool(client, "next_task", key)
    body = resp.json()
    assert body["task"]["id"] == ids["ready"] and body["task"]["stale"] is True
    assert body["counts"]["stale"] == 1
    resp = await _tool(client, "claim_task", key,
                       document_id=ids["ready"], agent_name="agent-b",
                       apply="auto")
    assert resp.json()["outcome"] == "taken_over"

    # A status outside ready/claimed → 409 not_ready; a headerless doc →
    # 409 not_a_task.
    await _patch_content(client, admin_token, ids["ready"], (
        "---\ntype: task\nstatus: review\n---\n"
    ))
    await _settle()
    resp = await _tool(client, "claim_task", key,
                       document_id=ids["ready"], agent_name="agent-a",
                       apply="auto")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "not_ready"
    plain = await _doc(client, admin_token, pid, "Plain", "no header")
    resp = await _tool(client, "claim_task", key,
                       document_id=plain, agent_name="agent-a", apply="auto")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "not_a_task"
    # A ready task assigned to someone else → 409 reserved naming them.
    held = await _doc(client, admin_token, pid, "Reserved task", (
        "---\ntype: task\nstatus: todo\nassignee: human\n---\n"
    ))
    resp = await _tool(client, "claim_task", key,
                       document_id=held, agent_name="agent-a", apply="auto")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "reserved"
    assert resp.json()["detail"]["assignee"] == "human"

    # The claim decides on the LIVE text, never the (lagging) projection: a
    # header claimed through REST a moment ago answers already_claimed before
    # the subscriber has run.
    live = await _doc(client, admin_token, pid, "Live-skew task", (
        "---\ntype: task\nstatus: todo\n---\n"
    ))
    await _patch_content(client, admin_token, live, (
        "---\ntype: task\nstatus: doing\nassignee: \"someone\"\n"
        "claimed_until: \"2030-01-01T00:00:00+00:00\"\n---\n"
    ))
    resp = await _tool(client, "claim_task", key,
                       document_id=live, agent_name="agent-a", apply="auto")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "already_claimed"

    # `under` narrows the candidates only: the root schema still governs a
    # subtree that holds no schema of its own.
    sub = await _doc(client, admin_token, pid, "Subtree root", "idx")
    sub_task = await _doc(client, admin_token, pid, "Subtree task", (
        "---\ntype: task\nstatus: todo\n---\n"
    ), parent_id=sub)
    await _settle()
    resp = await _tool(client, "next_task", key, under=sub)
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["id"] == sub_task

    # Two claim_task calls racing on one fresh task must produce exactly one
    # claimed and one already_claimed — the cross-replica edit_lock around
    # the read-decide-write is what makes the second caller re-read the
    # first's header instead of overwriting it.
    race = await _doc(client, admin_token, pid, "Race task", (
        "---\ntype: task\nstatus: todo\n---\n"
    ))
    await _settle()
    outcomes = await asyncio.gather(
        _tool(client, "claim_task", key,
              document_id=race, agent_name="agent-a", apply="auto"),
        _tool(client, "claim_task", key,
              document_id=race, agent_name="agent-b", apply="auto"),
    )
    agents = ("agent-a", "agent-b")
    winners, codes = [], []
    for agent, resp in zip(agents, outcomes):
        assert resp.status_code in (200, 409), resp.text
        if resp.status_code == 200:
            winners.append(agent)
            codes.append(resp.json()["outcome"])
        else:
            codes.append(resp.json()["detail"]["code"])
    assert len(winners) == 1, codes
    assert sorted(codes) == ["already_claimed", "claimed"], codes
    props, _ = await _parse_row_header(race)
    assert props["assignee"] == winners[0]
    await _settle()


async def _set(client, key, doc_id, status, agent="kilo"):
    return await _tool(client, "set_task_status", key, document_id=doc_id,
                       status=status, agent_name=agent, apply="auto")


def _code(resp) -> str:
    assert resp.status_code == 409, resp.text
    return resp.json()["detail"]["code"]


async def _children(test_db, parent_id) -> list[str]:
    from documents.service import sibling_rows

    row = await fetch_one("documents", parent_id)
    return [s["id"] for s in await sibling_rows(row["project_id"], parent_id)]


# set_task_status is decided on the live header under the edit lock, and its
# close moves the task through the real tree command — the outcome table,
# the archive placement and the retry contract are what the lore-health CLI
# reads.
# TEST-WHY: cross-process-contract — the lore-health CLI reads set_task_status outcomes and refusals
async def test_set_task_status_outcomes(client, test_db, collab_project):
    pid, _shared, admin_token, _ut, admin_uid, _uu = collab_project
    archive = await _doc(client, admin_token, pid, "DONE", "archive")
    x = await _doc(client, admin_token, pid, "X", "old", parent_id=archive)
    await _doc(client, admin_token, pid, "Task schema", SCHEMA_HEADER.replace(
        "priority: [urgent, normal]\n",
        f"priority: [urgent, normal]\narchive: \"[DONE](doc:{archive})\"\n",
    ))
    task = "---\ntype: task\nstatus: todo\n---\n\nBody.\n"
    a = await _doc(client, admin_token, pid, "A", task)
    c = await _doc(client, admin_token, pid, "C", task)
    c1 = await _doc(client, admin_token, pid, "C1", task, parent_id=c)
    await _settle()
    key = await _key(admin_uid, pid)

    assert _code(await _set(client, key, a, "doing")) == "use_claim_task"
    assert _code(await _set(client, key, a, "wip")) == "unknown_status"

    # A live foreign lease refuses with the holder named, nothing written.
    resp = await _tool(client, "claim_task", key, document_id=a,
                       agent_name="kilo", apply="auto")
    assert resp.status_code == 200, resp.text
    resp = await _set(client, key, a, "review", agent="other")
    assert _code(resp) == "already_claimed"
    assert resp.json()["detail"]["assignee"] == "kilo"
    props, _ = await _parse_row_header(a)
    assert props["status"] == "doing"

    # Hand back: status replaced, claimed_until gone, assignee kept, a
    # checkpoint names the transition. The same call again → no_change.
    resp = await _set(client, key, a, "review")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "outcome": "set", "document_id": a, "status": "review",
        "previous_status": "doing", "archived_to": None,
    }
    props, err = await _parse_row_header(a)
    assert err is None and props["status"] == "review"
    assert props["assignee"] == "kilo" and "claimed_until" not in props
    assert _code(await _set(client, key, a, "review")) == "no_change"

    # Close a top-level task → it lands under the archive AFTER X.
    resp = await _set(client, key, a, "done")
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived_to"] == archive
    assert await _children(test_db, archive) == [x, a]
    assert _code(await _set(client, key, a, "done")) == "no_change"

    # Open subtasks block the close; a closed subtask stays in place; then
    # the parent moves with its subtree.
    await _settle()
    resp = await _set(client, key, c, "done")
    assert _code(resp) == "open_subtasks"
    assert [t["id"] for t in resp.json()["detail"]["subtasks"]] == [c1]
    resp = await _set(client, key, c1, "done")
    assert resp.status_code == 200 and resp.json()["archived_to"] is None
    assert await _children(test_db, c) == [c1]
    await _settle()
    resp = await _set(client, key, c, "done")
    assert resp.status_code == 200 and resp.json()["archived_to"] == archive
    assert await _children(test_db, archive) == [x, a, c]
    assert await _children(test_db, c) == [c1]

    # A close whose move did not happen (closed by hand, outside the archive)
    # is completed by the same-status call: only the move, no header write.
    e = await _doc(client, admin_token, pid, "E",
                   "---\ntype: task\nstatus: done\n---\n")
    await _settle()
    resp = await _set(client, key, e, "done")
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived_to"] == archive
    assert (await _children(test_db, archive))[-1] == e

    # An archive the key cannot see refuses before anything is written.
    # (the scoped key sees only the schema inside its subtree)
    sub = await _doc(client, admin_token, pid, "Scoped", "scope root")
    await _doc(client, admin_token, pid, "Scoped schema", SCHEMA_HEADER.replace(
        "priority: [urgent, normal]\n",
        f"priority: [urgent, normal]\narchive: \"[DONE](doc:{archive})\"\n",
    ), parent_id=sub)
    d = await _doc(client, admin_token, pid, "D", task, parent_id=sub)
    await _settle()
    scoped = await _key(admin_uid, pid, scope_root=sub)
    assert _code(await _set(client, scoped, d, "done")) == "archive_not_visible"
    props, _ = await _parse_row_header(d)
    assert props["status"] == "todo"


# The refused principals: a read-only MCP key never reaches the claim; the
# admin tombstone closes all three tools on every surface; a hostile
# agent_name is a 422 that writes nothing; the default apply mode holds the
# claim for the approval card instead of applying it.
# TEST-WHY: refused-principal — the walls, not the happy path.
async def test_task_tools_refused_principals(client, mcp_running, test_db,
                                             collab_project):
    pid, admin_token, admin_uid, key, _schema_id, ids = await _seed_project(
        client, test_db, collab_project,
    )

    # A newline or colon in agent_name would inject header keys → 422, and
    # the header is unchanged (no checkpoint, no write).
    for bad in ("evil\nassignee: root", "a: b", "_x", "x" * 65):
        resp = await _tool(client, "claim_task", key,
                           document_id=ids["ready"], agent_name=bad,
                           apply="auto")
        assert resp.status_code == 422, (bad, resp.text)
    props, _ = await _parse_row_header(ids["ready"])
    assert "assignee" not in props

    # The default apply mode holds the claim: 409 confirmation_required, the
    # driver's approval ask — never a silent apply.
    resp = await _tool(client, "claim_task", key,
                       document_id=ids["ready"], agent_name="agent-a")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "confirmation_required"

    # A read-only MCP key (no auto_apply): reads pass, the claim is a 403 at
    # dispatch, and init surfaces the shipped tasks skill RAW.
    from db import create_record

    ro_token = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"ro-{secrets.token_hex(4)}", {
        "user_id": admin_uid, "project_id": pid, "document_id": "",
        "token_hash": hashlib.sha256(ro_token.encode()).hexdigest(),
        "label": "ro", "capabilities": ["agent"],
    })
    headers = {"Authorization": f"Bearer {ro_token}", "Accept": "application/json"}

    def _rpc(name, arguments):
        return {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments}}

    resp = await client.post("/mcp", json=_rpc("next_task", {}), headers=headers)
    assert resp.status_code == 200 and not resp.json()["result"].get("isError")
    resp = await client.post("/mcp", json=_rpc("claim_task", {
        "document_id": ids["ready"], "agent_name": "agent-a",
    }), headers=headers)
    payload = json.loads(resp.json()["result"]["content"][0]["text"])
    assert payload["status_code"] == 403, payload

    resp = await client.post("/mcp", json=_rpc("init", {}), headers=headers)
    assert resp.status_code == 200, resp.text
    init_payload = json.loads(resp.json()["result"]["content"][0]["text"])
    from agent_skills import shipped_skill_docs
    shipped = {d["location"]: d["content"] for d in shipped_skill_docs()}
    assert init_payload["shipped_skills"]["tasks"] == shipped["shipped:skill_tasks"]

    # The admin tombstone (a real PUT now that the skill ships) closes all
    # three tools on every surface; DELETE resets it.
    resp = await client.put("/api/admin/skills/tasks", json={"enabled": False},
                            cookies={"lore_session": admin_token})
    assert resp.status_code == 200, resp.text
    try:
        for name, body in (
            ("find_documents", {"where": {"type": "task"}}),
            ("next_task", {}),
            ("claim_task", {"document_id": ids["ready"], "agent_name": "a",
                            "apply": "auto"}),
            ("set_task_status", {"document_id": ids["ready"],
                                 "status": "review", "agent_name": "a",
                                 "apply": "auto"}),
        ):
            resp = await _tool(client, name, key, **body)
            assert resp.status_code == 403, (name, resp.text)
            assert resp.json()["detail"] == "tasks is disabled by the administrator"
    finally:
        resp = await client.delete("/api/admin/skills/tasks",
                                   cookies={"lore_session": admin_token})
        assert resp.status_code == 200, resp.text

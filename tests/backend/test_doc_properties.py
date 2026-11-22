"""Document properties (SYSTEM: doc-properties): the ONE header grammar (pure edges), the content_flushed projection
(the real subscriber, driven by the real emit), and find_documents' visibility
walls and typed equality.
"""
import asyncio

from db import fetch_one


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


# The grammar is the ONE splitter every header consumer shares
# (parse_persona imports it back; the projection and find_documents answer
# from it), and each edge here is decided by exactly one branch of it.
# TEST-WHY: pure-logic-edge — one shared splitter, one branch per edge.
def test_header_grammar_edges():
    from doc_properties import parse_header

    # No header — a `---` later in the body is prose, never a header.
    assert parse_header("Just prose\n---\nnot a header\n") == (None, None)
    assert parse_header("") == (None, None)
    # Plain header.
    props, err = parse_header("---\ntype: task\nstatus: todo\n---\nbody\n")
    assert err is None
    assert props == {"type": "task", "status": "todo"}
    # Unterminated block → no header (absent, not failed).
    assert parse_header("---\ntype: task\n") == (None, None)
    # Non-mapping block → visible failure.
    props, err = parse_header("---\njust a string\n---\n")
    assert props is None and "mapping" in err
    # Over-8KB block → refused before the YAML parser sees it.
    props, err = parse_header("---\nkey: " + "x" * 9000 + "\n---\n")
    assert props is None and "8192" in err
    # Dates/datetimes normalize to ISO strings; a naive datetime is UTC.
    props, err = parse_header(
        "---\ndue: 2026-11-22\nat: 2026-11-22T10:00:00\n"
        "offset: 2026-11-22T10:00:00+03:00\nlist: [2026-11-22]\n---\n"
    )
    assert err is None
    assert props == {
        "due": "2026-11-22", "at": "2026-11-22T10:00:00+00:00",
        "offset": "2026-11-22T10:00:00+03:00", "list": ["2026-11-22"],
    }
    # Trailing spaces on the fences and CRLF are tolerated.
    props, err = parse_header("--- \r\ntype: task\r\n--- \r\nbody")
    assert props == {"type": "task"} and err is None
    # An empty block is an empty mapping (persona parity).
    props, err = parse_header("---\n---\nbody")
    assert props == {} and err is None
    # A YAML failure is visible, not dropped (the unquoted-Lore-link trap).
    props, err = parse_header("---\ndepends: [Схема](doc:x)\n---\n")
    assert props is None and "malformed header YAML" in err
    # Non-string keys become their scalar spelling (SurrealDB objects take
    # string keys only); a value with no JSON shape is a visible failure.
    props, err = parse_header(
        "---\nhistory:\n  2026-11-20: created\n1: one\nnull: none-key\n---\n"
    )
    assert err is None
    assert props == {
        "history": {"2026-11-20": "created"}, "1": "one", "null": "none-key",
    }
    props, err = parse_header("---\ntags: !!set {a: null}\n---\n")
    assert props is None and "no JSON shape" in err


# The projection is written by an event-bus subscriber at flush time and read
# back from the row a query will see; driving the REAL emit (never a direct
# subscriber call) is what proves the subscription is registered at import in
# every process that flushed.
# TEST-WHY: cross-process-contract — bus task writes, row read answers.
async def test_projection_subscriber(client, test_db, collab_project):
    from doc_properties import parse_header  # noqa: F401 — grammar pinned above

    pid, doc_id, admin_token, *_ = collab_project

    await _patch_content(client, admin_token, doc_id,
                         "---\ntype: task\nstatus: todo\n---\n\nFix the thing\n")
    await _settle()
    row = await fetch_one("documents", doc_id)
    # A `SET field = NONE` projection REMOVES the key in SurrealDB, so the
    # absent halves read through .get() — absent IS the NONE projection.
    assert row.get("props") == {"type": "task", "status": "todo"}
    assert row.get("props_error") is None

    # Header removed → the projection clears (absent, not stale).
    await _patch_content(client, admin_token, doc_id, "Plain now\n")
    await _settle()
    row = await fetch_one("documents", doc_id)
    assert row.get("props") is None and row.get("props_error") is None

    # Broken header → props dropped, the reason kept.
    await _patch_content(client, admin_token, doc_id,
                         "---\ndepends: [Схема](doc:x)\n---\nbody\n")
    await _settle()
    row = await fetch_one("documents", doc_id)
    assert row.get("props") is None
    assert "malformed header YAML" in row["props_error"]

    # A reference with a header is source material, not a task: never projected.
    ref_id = await _doc(client, admin_token, pid, "Headed reference",
                        "---\ntype: task\n---\n", parent_id=doc_id,
                        is_reference=True, media_type="markdown")
    await _settle()
    row = await fetch_one("documents", ref_id)
    assert row.get("props") is None and row.get("props_error") is None


# The visibility function is a decision, not search parity: a subtree key sees
# only its subtree, the key owner's PIN-locked page is absent (a hide, not a
# refusal that leaks the count), the skill tombstone closes the tool on every
# surface, and equality is typed (1 is not "1") so a where miss is a mismatch,
# never a coercion.
# TEST-WHY: refused-principal — the walls, not the happy path.
async def test_find_documents_visibility_and_equality(client, test_db, collab_project):
    pid, _shared, admin_token, _user_token, admin_uid, _user_uid = collab_project

    sub_root = await _doc(client, admin_token, pid, "Subtree root", "idx")
    t1 = await _doc(client, admin_token, pid, "Task inside", (
        "---\ntype: task\nstatus: todo\npriority: 1\ndone: true\n"
        "due: 2026-11-22\n---\nbody\n"
    ), parent_id=sub_root)
    t2 = await _doc(client, admin_token, pid, "Task outside", (
        "---\ntype: task\nstatus: doing\n---\nbody\n"
    ))
    broken = await _doc(client, admin_token, pid, "Broken task", (
        "---\ndepends: [Схема](doc:x)\n---\nbody\n"
    ))
    await _settle()

    key = await _key(admin_uid, pid)

    resp = await _tool(client, "find_documents", key, where={"type": "task"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["id"] for r in body["results"]] == [t1, t2]  # oldest first
    assert [e["id"] for e in body["header_errors"]] == [broken]
    assert body["header_errors"][0]["error"].startswith("malformed header YAML")

    # Typed equality: exact on the JSON value, dates as ISO strings.
    for where, expected in (
        ({"type": "task", "status": "todo"}, [t1]),
        ({"priority": 1}, [t1]),
        ({"priority": "1"}, []),          # string never matches int
        ({"done": True}, [t1]),
        ({"done": "true"}, []),           # string never matches bool
        ({"due": "2026-11-22"}, [t1]),
        ({"status": ["todo", "doing"]}, [t1, t2]),
        ({}, [t1, t2]),                   # absent where = every headed doc
    ):
        resp = await _tool(client, "find_documents", key, where=where)
        assert [r["id"] for r in resp.json()["results"]] == expected, where

    # A bad key/value shape is a 422 the caller's loop self-corrects from.
    for where in ({"тип": "task"}, {"status": {}}, {"status": []}):
        resp = await _tool(client, "find_documents", key, where=where)
        assert resp.status_code == 422, (where, resp.text)

    # The subtree key: only its subtree, same broken-header reporting.
    key_sub = await _key(admin_uid, pid, scope_root=sub_root)
    resp = await _tool(client, "find_documents", key_sub, where={"type": "task"})
    assert [r["id"] for r in resp.json()["results"]] == [t1]
    # `under` narrows a whole-project key; out-of-scope is a 403 naming the wall.
    resp = await _tool(client, "find_documents", key,
                       where={"type": "task"}, under=sub_root)
    assert [r["id"] for r in resp.json()["results"]] == [t1]

    # The key owner's PIN-locked page is absent, not refused.
    assert (await client.patch("/api/cabinet/pin", json={"pin": "4321"},
                               cookies={"lore_session": admin_token})).status_code == 200
    assert (await client.post(f"/api/cabinet/locked-docs/{t2}",
                              cookies={"lore_session": admin_token})).status_code == 200
    resp = await _tool(client, "find_documents", key, where={"type": "task"})
    assert [r["id"] for r in resp.json()["results"]] == [t1]

    # The admin's tombstone closes the tool before any other answer (the row
    # is written directly; the admin PUT path is driven in
    # test_task_convention).
    await test_db.query(
        "UPSERT type::record('instance_skills', 'tasks') SET "
        "name = 'tasks', content = NONE, enabled = false, "
        "updated_by = $by, updated_at = time::now()",
        {"by": admin_uid},
    )
    try:
        resp = await _tool(client, "find_documents", key, where={"type": "task"})
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == "tasks is disabled by the administrator"
    finally:
        await test_db.query("DELETE type::record('instance_skills', 'tasks')")

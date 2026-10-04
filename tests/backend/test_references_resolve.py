"""POST /api/references/resolve + GET /api/references?q=.

resolve is the editor's ref: link validity probe: given (project_id, ids) it
returns the rows among `ids` that are live references of that project.

Contract (pinned in resolve_references): missing ids are ABSENT, never a 404 — an empty list
is a valid answer, so a broken link is indistinguishable from "no access" and no
existence oracle leaks. Archived refs are INCLUDED (an archived reference still
exists; its link must not read as broken), soft-deleted are absent, and a
foreign-project id is filtered by the `project_id` SQL clause.

`q` (project branch only) is the bounded server title search that replaced the
client's capped project-wide list in the link-suggestions popup.
"""

import pytest


async def _make_ref(client, token, pid, doc_id, title, content="body"):
    resp = await client.post(
        "/api/references",
        json={"project_id": pid, "document_id": doc_id, "title": title, "content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["reference_id"]


# ─── POST /api/references/resolve ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_returns_only_existing_project_refs(client, admin_user, project_with_doc):
    """Existing refs among ids are returned; an unknown id is simply absent."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_a = await _make_ref(client, token, pid, doc_id, "RefA")
    ref_b = await _make_ref(client, token, pid, doc_id, "RefB")
    resp = await client.post(
        "/api/references/resolve",
        json={"project_id": pid, "ids": [ref_a, "no-such-ref", ref_b]},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    ids = {r["reference_id"] for r in resp.json()}
    assert ids == {ref_a, ref_b}


@pytest.mark.asyncio
async def test_resolve_all_missing_returns_empty_list_not_404(client, admin_user, project_with_doc):
    """The probe's normal answer includes "missing" — an empty list, never 404."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/references/resolve",
        json={"project_id": pid, "ids": ["nope-1", "nope-2"]},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_resolve_includes_archived(client, admin_user, project_with_doc):
    """An archived reference still exists — its link must not read as broken."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _make_ref(client, token, pid, doc_id, "ArchivedRef")
    await client.patch(
        f"/api/references/{ref_id}", json={"archived": True}, cookies={"lore_session": token}
    )
    resp = await client.post(
        "/api/references/resolve",
        json={"project_id": pid, "ids": [ref_id]},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    ids = [r["reference_id"] for r in resp.json()]
    assert ids == [ref_id]


@pytest.mark.asyncio
async def test_resolve_excludes_soft_deleted(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_id = await _make_ref(client, token, pid, doc_id, "DeletedRef")
    resp = await client.delete(f"/api/references/{ref_id}", cookies={"lore_session": token})
    assert resp.status_code == 200
    resp = await client.post(
        "/api/references/resolve",
        json={"project_id": pid, "ids": [ref_id]},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_resolve_foreign_project_id_absent(client, admin_user, project_with_doc):
    """A ref of another project is filtered by the project_id clause — absent,
    indistinguishable from missing (no cross-project existence oracle)."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/projects", json={"name": "Other Project"}, cookies={"lore_session": token}
    )
    assert resp.status_code == 200, resp.text
    other_pid = resp.json()["project_id"]
    foreign_ref = await _make_ref(client, token, other_pid, None, "ForeignRef")
    resp = await client.post(
        "/api/references/resolve",
        json={"project_id": pid, "ids": [foreign_ref]},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_resolve_without_project_read_404s(client, regular_user, project_with_doc):
    """A user without read access gets require_project_read's answer (404), not rows."""
    pid, _, _ = project_with_doc
    _, token = regular_user
    resp = await client.post(
        "/api/references/resolve",
        json={"project_id": pid, "ids": ["any-id"]},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_resolve_201_ids_rejected(client, admin_user, project_with_doc):
    """ids is capped at 200 (the client chunks); one over is a 422."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    resp = await client.post(
        "/api/references/resolve",
        json={"project_id": pid, "ids": [f"id-{i}" for i in range(201)]},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422


# ─── GET /api/references?q= (project-branch title search) ───────────────────


@pytest.mark.asyncio
async def test_q_filters_title_case_insensitively(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    await _make_ref(client, token, pid, doc_id, "Alpha Note")
    await _make_ref(client, token, pid, doc_id, "beta NOTE")
    await _make_ref(client, token, pid, doc_id, "gamma other")
    resp = await client.get(
        f"/api/references?project_id={pid}&q=ALPHA", cookies={"lore_session": token}
    )
    titles = [r["title"] for r in resp.json()]
    assert titles == ["Alpha Note"]
    resp = await client.get(
        f"/api/references?project_id={pid}&q=note", cookies={"lore_session": token}
    )
    titles = sorted(r["title"] for r in resp.json())
    assert titles == ["Alpha Note", "beta NOTE"]


@pytest.mark.asyncio
async def test_q_honours_limit(client, admin_user, project_with_doc):
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    for i in range(3):
        await _make_ref(client, token, pid, doc_id, f"Match {i}")
    resp = await client.get(
        f"/api/references?project_id={pid}&q=Match&limit=2", cookies={"lore_session": token}
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_q_empty_returns_most_recently_updated(client, admin_user, project_with_doc):
    """q present but empty = the bounded default page: the most recently updated refs."""
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    await _make_ref(client, token, pid, doc_id, "Older")
    await _make_ref(client, token, pid, doc_id, "Newer")
    resp = await client.get(
        f"/api/references?project_id={pid}&q=&limit=50", cookies={"lore_session": token}
    )
    assert resp.status_code == 200
    titles = [r["title"] for r in resp.json()]
    assert titles[0] == "Newer"


@pytest.mark.asyncio
async def test_q_absent_keeps_panel_order(client, admin_user, project_with_doc):
    """No q param = the legacy panel order (manual sort_key), not updated_at DESC.

    Pinned relationally: the relative order of two same-host refs in the no-q
    project LIST equals their order in the doc-scope LIST (both are the manual
    (sort_key, id) order — the q branch would instead order by updated_at DESC,
    which for two refs created in sequence is the reverse of the top-insert
    sort_key order).
    """
    pid, doc_id, _ = project_with_doc
    _, token = admin_user
    ref_a = await _make_ref(client, token, pid, doc_id, "First")
    ref_b = await _make_ref(client, token, pid, doc_id, "Second")
    by_doc = (await client.get(
        f"/api/references?document_id={doc_id}", cookies={"lore_session": token}
    )).json()
    by_proj = (await client.get(
        f"/api/references?project_id={pid}", cookies={"lore_session": token}
    )).json()
    doc_order = [r["reference_id"] for r in by_doc if r["reference_id"] in {ref_a, ref_b}]
    proj_order = [r["reference_id"] for r in by_proj if r["reference_id"] in {ref_a, ref_b}]
    assert set(doc_order) == {ref_a, ref_b}
    assert doc_order == proj_order

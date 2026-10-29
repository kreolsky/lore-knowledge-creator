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


# ─── GET /api/references?q= (project-branch title search) ───────────────────


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

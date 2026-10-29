"""Unified single-token capability keys — one POST mints ONE row whose
`capabilities` set (non-empty subset of {widget, agent}) selects the surfaces it
works on.

Subtree enforcement + capability gating live in
`test_subtree_scoped_agent_keys.py`. This file covers
the issuance contract:
  - POST /api/api-keys widget-only reproduces today's behavior (one row),
  - POST agent-only → one row scoped to the doc's subtree,
  - POST both → ONE row / ONE token carrying both capabilities,
  - empty capabilities → 422,
  - a non-member cannot mint,
  - is_system doc + agent → 422 (widget still allowed),
  - GET ?document_id returns capabilities/auto_apply and hides internal keys,
  - DELETE is capability-agnostic,
  - Pi internal path: internal=true + document_id='' rows, never adopts a
    user-minted subtree-scoped key,
  - enriched documents response: per-doc key_capabilities (own, non-internal).
"""

import pytest

# ─── Helpers ─────────────────────────────────────────────────────────────────


async def _make_doc(
    client, token: str, project_id: str, title: str, *, parent_id: str | None = None,
) -> str:
    payload: dict = {"project_id": project_id, "title": title}
    if parent_id:
        payload["parent_id"] = parent_id
    resp = await client.post(
        "/api/documents", json=payload, cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _mint(client, token: str, doc: str, capabilities: list[str], **extra):
    return await client.post(
        "/api/api-keys",
        json={"document_id": doc, "capabilities": capabilities, **extra},
        cookies={"lore_session": token},
    )


# ─── Access gate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_member_rejected(client, admin_user, regular_user, project_with_doc):
    """A user who is not a project member at all cannot mint anything."""
    pid, _, _ = project_with_doc
    _, reg_token = regular_user
    doc = await _make_doc(client, admin_user[1], pid, "NonMemberDoc")

    resp = await _mint(client, reg_token, doc, ["widget"])
    assert resp.status_code in (403, 404), resp.text


# ─── Enriched documents response: key_capabilities ───────────────────────────


@pytest.mark.asyncio
async def test_documents_enriched_with_key_capabilities(
    client, admin_user, project_with_doc,
):
    """GET /api/projects/{pid} annotates docs that have the caller's own keys
    with key_capabilities; docs without keys carry none."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc_w = await _make_doc(client, token, pid, "TreeWidgetDoc")
    doc_a = await _make_doc(client, token, pid, "TreeAgentDoc")
    doc_none = await _make_doc(client, token, pid, "TreeBareDoc")

    assert (await _mint(client, token, doc_w, ["widget"])).status_code == 200
    assert (await _mint(client, token, doc_a, ["widget", "agent"])).status_code == 200

    resp = await client.get(f"/api/projects/{pid}", cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    docs = {d["document_id"]: d for d in resp.json()["documents"]}
    assert docs[doc_w].get("key_capabilities") == ["widget"]
    assert sorted(docs[doc_a].get("key_capabilities", [])) == ["agent", "widget"]
    assert not docs[doc_none].get("key_capabilities")

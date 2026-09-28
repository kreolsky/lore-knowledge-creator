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
import hashlib
import secrets

import pytest

from db import create_record, get_db

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


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


async def _make_system_doc(project_id: str, *, title: str = "brain") -> str:
    """Insert a system document (is_system=true) directly into the DB."""
    doc_id = f"sysdoc-{secrets.token_hex(4)}"
    await create_record("documents", doc_id, {
        "project_id": project_id, "parent_id": None, "title": title,
        "content": "", "path": title, "is_system": True, "system_role": "brain",
    })
    return doc_id


async def _add_member(project_id: str, user_id: str, level: str = "readonly") -> str:
    """Create/replace a project_members row and return its id."""
    pm_id = f"pm-{project_id}-{user_id}"
    db = await get_db()
    await db.query(
        "DELETE type::record('project_members', $id)", {"id": pm_id},
    )
    await create_record("project_members", pm_id, {
        "project_id": project_id, "user_id": user_id, "access_level": level,
    })
    return pm_id


async def _count_keys(*, user_id: str | None = None, document_id: str | None = None) -> int:
    """Count non-deleted api_keys rows matching the filters."""
    db = await get_db()
    clauses = ["deleted_at IS NONE"]
    params: dict = {}
    if user_id is not None:
        clauses.append("user_id = $uid")
        params["uid"] = user_id
    if document_id is not None:
        clauses.append("document_id = $did")
        params["did"] = document_id
    rows = await db.query(
        "SELECT count() AS n FROM api_keys WHERE " + " AND ".join(clauses)
        + " GROUP ALL",
        params,
    )
    if not rows:
        return 0
    return int(rows[0].get("n", 0))


async def _mint(client, token: str, doc: str, capabilities: list[str], **extra):
    return await client.post(
        "/api/api-keys",
        json={"document_id": doc, "capabilities": capabilities, **extra},
        cookies={"lore_session": token},
    )


# ─── POST: widget-only (today's behavior preserved) ──────────────────────────


@pytest.mark.asyncio
async def test_post_widget_only_mints_single_row(client, admin_user, project_with_doc):
    """capabilities=['widget'] mints exactly one row bound to the doc."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc = await _make_doc(client, token, pid, "DefaultDoc")

    resp = await _mint(client, token, doc, ["widget"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["capabilities"] == ["widget"]
    assert body["document_id"] == doc
    assert body["token"].startswith("lore_")
    assert body["key_id"]
    # Scope to THIS test's doc, not the shared admin uid: admin_user yields a fixed
    # uid reused across the suite, so a user-scoped count is not test-local and inflates
    # under -n 4 (a sibling test's key leaking in). One POST mints one doc-bound row.
    assert await _count_keys(document_id=doc) == 1


@pytest.mark.asyncio
async def test_post_without_ttl_never_expires(client, admin_user, project_with_doc):
    """A key minted without expires_in_days carries NO expiry — neither in the
    response nor in the stored row. Regression: a 90-day default silently
    expired long-lived widget/mobile keys on prod (they have no re-issue flow).
    """
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc = await _make_doc(client, token, pid, "NoTtlDoc")

    body = (await _mint(client, token, doc, ["widget"])).json()
    assert body["expires_at"] is None

    db = await get_db()
    rows = await db.query(
        "SELECT expires_at FROM api_keys WHERE document_id = $d", {"d": doc},
    )
    assert rows and all(r.get("expires_at") is None for r in rows), rows


# ─── POST: agent only ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_agent_only_mints_scoped_row(client, admin_user, project_with_doc):
    """capabilities=['agent'] mints one row whose document_id (= subtree root)
    is the issuing doc; auto_apply persists."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc = await _make_doc(client, token, pid, "AgentDoc")

    resp = await _mint(client, token, doc, ["agent"], auto_apply=True)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["capabilities"] == ["agent"]
    assert body["document_id"] == doc
    assert body["auto_apply"] is True
    assert body["token"].startswith("lore_")
    # Doc-scoped (see test_post_widget_only_mints_single_row) — isolation-proof under -n 4.
    assert await _count_keys(document_id=doc) == 1


# ─── POST: both capabilities → ONE row, ONE token ────────────────────────────


@pytest.mark.asyncio
async def test_post_both_capabilities_mints_one_row(client, admin_user, project_with_doc):
    """Both capabilities → a single row / single token carrying both."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc = await _make_doc(client, token, pid, "BothDoc")

    resp = await _mint(client, token, doc, ["widget", "agent"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(body["capabilities"]) == ["agent", "widget"]
    assert body["token"].startswith("lore_")
    # Doc-scoped (see test_post_widget_only_mints_single_row) — isolation-proof under -n 4.
    assert await _count_keys(document_id=doc) == 1


@pytest.mark.asyncio
async def test_post_empty_capabilities_rejected(client, admin_user, project_with_doc):
    """capabilities=[] is a validation error (non-empty subset required)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc = await _make_doc(client, token, pid, "EmptyCapDoc")

    resp = await _mint(client, token, doc, [])
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_post_unknown_capability_rejected(client, admin_user, project_with_doc):
    """Unknown capability values are rejected."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc = await _make_doc(client, token, pid, "BadCapDoc")

    resp = await _mint(client, token, doc, ["widget", "root"])
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_widget_only_auto_apply_rejected(client, admin_user, project_with_doc):
    """auto_apply is meaningless without the agent capability (it is the MCP
    gateway's per-key write ceiling). A widget-only key with auto_apply=True is
    rejected with 422 at issuance — NOT silently dropped — so a client bug
    (asking for auto-write on a surface that can't write) surfaces loudly instead
    of being masked (no-silent-degradation, applied to the API contract)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc = await _make_doc(client, token, pid, "AutoApplyWidgetDoc")

    resp = await _mint(client, token, doc, ["widget"], auto_apply=True)
    assert resp.status_code == 422, resp.text


# ─── Access gate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_member_rejected(client, admin_user, regular_user, project_with_doc):
    """A user who is not a project member at all cannot mint anything."""
    pid, _, _ = project_with_doc
    _, reg_token = regular_user
    doc = await _make_doc(client, admin_user[1], pid, "NonMemberDoc")

    resp = await _mint(client, reg_token, doc, ["widget"])
    assert resp.status_code in (403, 404), resp.text


# ─── is_system doc + agent → 422 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_is_system_doc_rejects_agent(client, admin_user, project_with_doc):
    """A system document cannot be an agent subtree scope root (422)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    sys_doc = await _make_system_doc(pid)

    resp = await _mint(client, token, sys_doc, ["agent"])
    assert resp.status_code == 422, resp.text
    resp_both = await _mint(client, token, sys_doc, ["widget", "agent"])
    assert resp_both.status_code == 422, resp_both.text


@pytest.mark.asyncio
async def test_is_system_doc_allows_widget(client, admin_user, project_with_doc):
    """Widget-only keys can still be minted on a system doc (single-doc binding,
    not a subtree scope)."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    sys_doc = await _make_system_doc(pid)

    resp = await _mint(client, token, sys_doc, ["widget"])
    assert resp.status_code == 200, resp.text


# ─── GET ?document_id: capabilities + internal exclusion ─────────────────────


@pytest.mark.asyncio
async def test_list_returns_capabilities(client, admin_user, project_with_doc):
    """GET /api-keys?document_id=X returns each key's capabilities + auto_apply."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc = await _make_doc(client, token, pid, "ListDoc")

    resp = await _mint(client, token, doc, ["widget", "agent"], auto_apply=True)
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/api/api-keys?document_id={doc}", cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert len(rows) == 1
    assert sorted(rows[0]["capabilities"]) == ["agent", "widget"]
    assert rows[0]["auto_apply"] is True


@pytest.mark.asyncio
async def test_list_excludes_internal_keys(client, admin_user, project_with_doc):
    """Internal (Pi-minted) keys never appear in user-facing key lists.

    An internal key has document_id='' so it can't collide with a doc-scoped
    list anyway — pin the contract with an internal key that (incorrectly)
    carries a document_id: the internal flag alone must exclude it."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    doc = await _make_doc(client, token, pid, "InternalDoc")

    tok = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"internal-{secrets.token_hex(4)}", {
        "user_id": admin_uid, "project_id": pid, "document_id": doc,
        "token_hash": hashlib.sha256(tok.encode()).hexdigest(),
        "label": "agent", "capabilities": ["agent"], "internal": True,
    })

    resp = await client.get(
        f"/api/api-keys?document_id={doc}", cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


# ─── DELETE is capability-agnostic ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_combined_key(client, admin_user, project_with_doc):
    """DELETE /api-keys/{id} soft-deletes a combined-capability key."""
    pid, _, _ = project_with_doc
    _, token = admin_user
    doc = await _make_doc(client, token, pid, "DelDoc")

    resp = await _mint(client, token, doc, ["widget", "agent"])
    key_id = resp.json()["key_id"]

    resp = await client.delete(
        f"/api/api-keys/{key_id}", cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    assert await _count_keys(document_id=doc) == 0


# ─── Agent internal path ────────────────────────────────────────────────────────


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


@pytest.mark.asyncio
async def test_documents_enrichment_own_and_non_internal_only(
    client, admin_user, regular_user, project_with_doc,
):
    """Another member's keys and internal (Pi) keys never surface as
    key_capabilities in the caller's documents payload."""
    pid, _, admin_uid = project_with_doc
    _, admin_token = admin_user
    reg_uid, reg_token = regular_user
    doc = await _make_doc(client, admin_token, pid, "PrivacyDoc")
    await _add_member(pid, reg_uid, level="readonly")

    # Admin's user key + an internal key pointing at the doc.
    assert (await _mint(client, admin_token, doc, ["widget"])).status_code == 200
    tok = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"internal-{secrets.token_hex(4)}", {
        "user_id": reg_uid, "project_id": pid, "document_id": doc,
        "token_hash": hashlib.sha256(tok.encode()).hexdigest(),
        "label": "agent", "capabilities": ["agent"], "internal": True,
    })

    resp = await client.get(f"/api/projects/{pid}", cookies={"lore_session": reg_token})
    assert resp.status_code == 200, resp.text
    docs = {d["document_id"]: d for d in resp.json()["documents"]}
    # regular_user sees NEITHER admin's key nor the internal key.
    assert not docs[doc].get("key_capabilities")

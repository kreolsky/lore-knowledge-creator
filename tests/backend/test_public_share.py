"""Anonymous public share — read-only links over a single doc or subtree
(plan "iridescent-wibbling-heron").

Two new subsystems:
  - SYSTEM: document-shares — owner-gated CRUD (mint/list/revoke share tokens).
  - SYSTEM: public-share    — anonymous read router (no get_current_user) with a
    single funnel `resolve_share(token)` → (project_id, root, scope, doc_ids);
    every endpoint asserts the requested doc ∈ doc_ids.

Covers the REFUSED principal (out-of-scope doc, write verb, deleted/invalid
token), the live "move" semantics (a doc moved under a shared root becomes
readable), the depth-50 descendant cap, and the per-IP rate limit.

DECISION-PIN: the share token is stored PLAINTEXT (it IS the public URL, not a
secret credential). See surreal/schema.surql for the rationale.
"""
import logging
from unittest.mock import AsyncMock, patch

import pytest

# ─── Helpers ─────────────────────────────────────────────────────────────────


async def _make_doc(
    client, token: str, project_id: str, title: str, content: str = "",
    *, parent_id: str | None = None,
) -> str:
    payload: dict = {"project_id": project_id, "title": title, "content": content}
    if parent_id:
        payload["parent_id"] = parent_id
    resp = await client.post(
        "/api/documents", json=payload, cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _make_ref(
    client, token: str, project_id: str, parent_doc: str,
    title: str, content: str = "",
) -> str:
    resp = await client.post(
        "/api/references",
        json={
            "project_id": project_id, "document_id": parent_doc,
            "title": title, "media_type": "markdown", "content": content,
        },
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["reference_id"]


async def _mint_share(
    client, token: str, project_id: str, document_id: str, scope: str,
) -> dict:
    resp = await client.post(
        f"/api/projects/{project_id}/documents/{document_id}/shares",
        json={"scope": scope},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _build_tree(client, admin_token, project_id):
    """root → child → grandchild, plus an outside sibling."""
    root = await _make_doc(client, admin_token, project_id, "Root", "root content here")
    child = await _make_doc(
        client, admin_token, project_id, "Child", "child content here",
        parent_id=root,
    )
    grandchild = await _make_doc(
        client, admin_token, project_id, "Grandchild", "grandchild content here",
        parent_id=child,
    )
    outside = await _make_doc(client, admin_token, project_id, "Outside", "outside content here")
    return root, child, grandchild, outside


# ─── CRUD: SYSTEM: document-shares (owner-gated) ───────────────────────────


@pytest.mark.asyncio
async def test_owner_mints_doc_share_stores_plaintext_token(client, admin_user, project_with_doc):
    """POST mints a doc-scope share; the plaintext token IS persisted verbatim.

    DECISION-PIN (plan override): a public share token is the public URL, not a
    secret credential — it is stored plaintext so the owner can re-copy/re-send
    the link anytime. See surreal/schema.surql for the full rationale.
    """
    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user

    data = await _mint_share(client, admin_token, pid, idx_id, "doc")

    assert data["scope"] == "doc"
    assert data["token"].startswith("lore_"), "plaintext token must be a lore_ secret"
    assert "share_id" in data and data["share_id"]

    # The plaintext token is persisted verbatim (NOT hashed) — DECISION-PIN.
    from db import get_db
    db = await get_db()
    rows = await db.query(
        "SELECT * FROM document_shares WHERE document_id = $did AND deleted_at IS NONE",
        {"did": idx_id},
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["token"] == data["token"], "plaintext token must be persisted verbatim"
    assert row["scope"] == "doc"
    assert row["project_id"] == pid
    assert row["created_by"] is not None


@pytest.mark.asyncio
async def test_owner_mints_subtree_share(client, admin_user, project_with_doc):
    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user
    data = await _mint_share(client, admin_token, pid, idx_id, "subtree")
    assert data["scope"] == "subtree"
    assert data["token"].startswith("lore_")


@pytest.mark.asyncio
async def test_non_owner_full_member_cannot_mint(client, admin_user, regular_user, project_with_doc):
    """INVARIANT: share writes stay owner-only (UI mirrors the API gate)."""
    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user
    user_uid, user_token = regular_user

    # Make the regular user a `full` project member.
    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "full"},
        cookies={"lore_session": admin_token},
    )

    resp = await client.post(
        f"/api/projects/{pid}/documents/{idx_id}/shares",
        json={"scope": "doc"},
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 403, f"non-owner full member must NOT mint: {resp.text}"


@pytest.mark.asyncio
async def test_non_owner_full_member_can_list_shares(client, admin_user, regular_user, project_with_doc):
    """INVARIANT: a non-owner `full` member SEES the Access tab sub-sections
    read-only — LIST shares must succeed (plan: writes owner-only, reads full).

    Regression guard: an earlier revision made LIST owner-only, surfacing a
    spurious "Failed to load members" toast for non-owner full users.
    """
    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user
    user_uid, user_token = regular_user

    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "full"},
        cookies={"lore_session": admin_token},
    )
    # Owner mints a share first.
    await _mint_share(client, admin_token, pid, idx_id, "doc")

    resp = await client.get(
        f"/api/projects/{pid}/documents/{idx_id}/shares",
        cookies={"lore_session": user_token},
    )
    assert resp.status_code == 200, f"non-owner full must LIST shares: {resp.text}"
    shares = resp.json()["shares"]
    assert len(shares) == 1
    # The plaintext token is returned (DECISION-PIN) so the plaque can copy the link.
    assert "token" in shares[0]


@pytest.mark.asyncio
async def test_list_shares_includes_token_for_copy(client, admin_user, project_with_doc):
    """LIST returns the plaintext token so the UI can render a copyable link plaque.

    DECISION-PIN: the token IS the public URL — re-sending it is the intended
    UX, so LIST (full-access) carries it. A full non-owner seeing the list can
    do nothing the URL didn't already grant (the content is public-by-link).
    """
    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user
    created = await _mint_share(client, admin_token, pid, idx_id, "doc")

    resp = await client.get(
        f"/api/projects/{pid}/documents/{idx_id}/shares",
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    shares = resp.json()["shares"]
    assert len(shares) == 1
    s = shares[0]
    assert s["scope"] == "doc"
    assert s["share_id"]
    # The plaintext token IS returned — the plaque can build + copy the link.
    assert s["token"] == created["token"]


@pytest.mark.asyncio
async def test_delete_share_soft_deletes(client, admin_user, project_with_doc):
    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user
    data = await _mint_share(client, admin_token, pid, idx_id, "doc")
    share_id = data["share_id"]

    resp = await client.delete(
        f"/api/shares/{share_id}",
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text

    # LIST no longer shows it.
    resp = await client.get(
        f"/api/projects/{pid}/documents/{idx_id}/shares",
        cookies={"lore_session": admin_token},
    )
    assert resp.json()["shares"] == []

    # Row still exists but is soft-deleted.
    from db import get_db
    db = await get_db()
    rows = await db.query(
        "SELECT deleted_at FROM document_shares WHERE id = type::record('document_shares', $id)",
        {"id": share_id},
    )
    assert rows and rows[0]["deleted_at"] is not None


# ─── Anonymous reads: SYSTEM: public-share ─────────────────────────────────


@pytest.mark.asyncio
async def test_public_tree_doc_scope_returns_root_only(client, admin_user, project_with_doc):
    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user
    root, child, grandchild, _ = await _build_tree(client, admin_token, pid)

    data = await _mint_share(client, admin_token, pid, root, "doc")
    token = data["token"]

    resp = await client.get(f"/api/public/{token}/tree")
    assert resp.status_code == 200, resp.text
    tree = resp.json()
    ids = {n["id"] for n in tree["nodes"]}
    assert ids == {root}, f"doc-scope must expose the root ONLY, got {ids}"


@pytest.mark.asyncio
async def test_public_tree_returns_project_name(client, admin_user, project_with_doc, test_db):
    """/tree discloses the owning project's name (public Header breadcrumb root).
    Assert over the DERIVED project row, not a literal (testing.md rule)."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)

    data = await _mint_share(client, admin_token, pid, root, "doc")
    token = data["token"]

    rows = await test_db.query("SELECT name FROM projects WHERE meta::id(id) = $pid", {"pid": pid})
    expected_name = rows[0]["name"]

    resp = await client.get(f"/api/public/{token}/tree")
    assert resp.status_code == 200, resp.text
    assert resp.json()["project_name"] == expected_name


@pytest.mark.asyncio
async def test_public_tree_subtree_scope_returns_root_plus_descendants(
    client, admin_user, project_with_doc,
):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, child, grandchild, outside = await _build_tree(client, admin_token, pid)

    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    resp = await client.get(f"/api/public/{token}/tree")
    assert resp.status_code == 200, resp.text
    ids = {n["id"] for n in resp.json()["nodes"]}
    # Assert over the DERIVED subtree, not a literal (testing.md rule).
    from scope import subtree_doc_ids
    expected = set(await subtree_doc_ids(root, pid))
    assert ids == expected, f"subtree-scope tree must equal derived doc_ids; got {ids}"
    assert outside not in ids, "outside doc must NOT appear in the subtree share"


@pytest.mark.asyncio
async def test_public_tree_nodes_carry_iso_dates(
    client, admin_user, project_with_doc,
):
    """Every tree node exposes created_at/updated_at as parseable ISO strings.

    WHY: a blog built on the public tree needs dates for ordering and feeds.
    Pins the OUTPUT (datetime.fromisoformat parses), not the input type —
    SurrealDB may hand the driver a datetime or a string depending on path.
    Also pins that no `*_fmt` key rides along (same encoding contract as
    serialize_record: ISO strings only, the client formats).
    """
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, child, grandchild, _ = await _build_tree(client, admin_token, pid)

    await _mint_share(client, admin_token, pid, root, "subtree")

    resp = await client.get(f"/api/public/documents/{root}/tree")
    assert resp.status_code == 200, resp.text
    nodes = resp.json()["nodes"]
    assert nodes, "tree must have nodes to assert over"
    from datetime import datetime
    for n in nodes:
        for key in ("created_at", "updated_at"):
            assert key in n, f"node {n['id']} missing {key}"
            assert datetime.fromisoformat(n[key]), f"node {n['id']} {key} not ISO"
        fmt_keys = [k for k in n if k.endswith("_fmt")]
        assert not fmt_keys, f"node {n['id']} carries preformatted keys: {fmt_keys}"


@pytest.mark.asyncio
async def test_public_tree_excludes_references_from_document_structure(
    client, admin_user, project_with_doc,
):
    """References are `documents` rows (is_reference=true, parent_id = owning doc)
    and thus appear as descendants in the scope walk — they must NOT render as tree
    nodes on the public document structure.

    Regression: subtree_doc_ids includes reference ids (they are descendants), so
    without an is_reference filter the public tree returned references mixed with
    real documents, and /s/:token rendered them in the document list. Mirrors the
    authed tree query (projects.py "AND is_reference = false"). References still
    reach the refs panel via the dedicated references endpoint (asserted here so a
    future over-broad filter can't silently drop ref access).
    """
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, child, _, _ = await _build_tree(client, admin_token, pid)
    ref_on_root = await _make_ref(
        client, admin_token, pid, root, "Root ref", "root ref body",
    )

    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    resp = await client.get(f"/api/public/{token}/tree")
    assert resp.status_code == 200, resp.text
    ids = {n["id"] for n in resp.json()["nodes"]}

    assert {root, child} <= ids, f"real docs missing from tree: {ids}"
    assert ref_on_root not in ids, (
        f"reference leaked into the public document tree: {ids}"
    )

    # The reference is still served via its dedicated endpoint (not broken).
    resp = await client.get(f"/api/public/{token}/documents/{root}/references")
    assert resp.status_code == 200, resp.text
    ref_ids = {r["reference_id"] for r in resp.json()["references"]}
    assert ref_on_root in ref_ids, "reference must still be served via the refs endpoint"


@pytest.mark.asyncio
async def test_public_document_returns_content_and_headings(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)

    data = await _mint_share(client, admin_token, pid, root, "doc")
    token = data["token"]

    resp = await client.get(f"/api/public/{token}/documents/{root}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "root content" in body["content"]
    assert "headings" in body
    assert body["document_id"] == root
    # tables_json: key present (the public Editor seeds table widgets from it).
    # Null is acceptable (no live Yjs state in test) — the key MUST exist so the FE
    # can read `body.tables_json ?? null`.
    assert "tables_json" in body


@pytest.mark.asyncio
async def test_public_document_503_when_capture_live_state_fails(
    client, admin_user, project_with_doc, caplog,
):
    """A capture_live_state throw means the Y.Doc layer is broken (Surreal down /
    CRDT decode failure) — never "legacy doc", because load() seeds from
    documents.content instead of raising when ydoc_state is absent. Serving
    documents.content there would show a reader content that lags the live doc by
    every uncompacted update. Refuse with 503 instead, and log the failure."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)
    data = await _mint_share(client, admin_token, pid, root, "doc")
    token = data["token"]

    with patch("ydoc_store.capture_live_state", new_callable=AsyncMock,
               side_effect=RuntimeError("yjs boom")):
        with caplog.at_level(logging.WARNING, logger="routes.public_share"):
            resp = await client.get(f"/api/public/{token}/documents/{root}")

    assert resp.status_code == 503, resp.text
    assert "root content" not in resp.text, "stale DB content must not leak in the error body"
    # The failure is observable, not silent.
    assert any(
        "capture_live_state failed" in r.message and root in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_public_document_without_ydoc_state_serves_db_content(
    client, admin_user, project_with_doc,
):
    """Premise pin for the 503 above: a doc that never entered collab has no
    ydoc_state row, and that must stay a 200 served from documents.content. If
    ydoc_store.load() ever starts raising for a missing state, every legacy public
    document silently turns into 503 — this test is the guard on that."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)
    data = await _mint_share(client, admin_token, pid, root, "doc")
    token = data["token"]

    from db import get_db
    db = await get_db()
    rows = await db.query(
        "SELECT ydoc_state FROM documents WHERE id = type::record('documents', $id)",
        {"id": root},
    )
    assert not rows[0].get("ydoc_state"), "fixture doc must have no persisted Y.Doc state"

    resp = await client.get(f"/api/public/{token}/documents/{root}")
    assert resp.status_code == 200, resp.text
    assert "root content" in resp.json()["content"]


@pytest.mark.asyncio
async def test_public_references_for_in_scope_docs(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, child, _, _ = await _build_tree(client, admin_token, pid)
    ref_on_root = await _make_ref(
        client, admin_token, pid, root, "Root ref", "# H1\nroot ref body",
    )
    ref_on_child = await _make_ref(
        client, admin_token, pid, child, "Child ref", "child ref body",
    )

    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    resp = await client.get(f"/api/public/{token}/documents/{root}/references")
    assert resp.status_code == 200, resp.text
    ref_ids = {r["reference_id"] for r in resp.json()["references"]}
    assert ref_on_root in ref_ids

    resp = await client.get(f"/api/public/{token}/documents/{child}/references")
    assert resp.status_code == 200, resp.text
    ref_ids = {r["reference_id"] for r in resp.json()["references"]}
    assert ref_on_child in ref_ids


@pytest.mark.asyncio
async def test_public_references_carry_content_for_transclusion(
    client, admin_user, project_with_doc,
):
    """The public references response MUST carry `content` so the FE
    `buildPublicTransclusionMap` can seed ref-text entries inline (the authed
    lazy fetch would 401 anonymously).

    WHY this test exists (regression guard for the SELECT * coupling): the
    authed LIST endpoint strips `content` via column projection
    (`_REF_META_SELECT` in references.py), NOT via the `_serialize_ref_meta`
    serializer — the serializer's docstring says "metadata-only, no content"
    but the function itself passes `content` through if present. The public
    router relies on that passthrough by issuing `SELECT *`.

    If a future "make the serializer match its docstring" change drops
    `content` from `_serialize_ref_meta`, public transclusions silently break
    with no failing test — EXCEPT this one. Bind the contract here.
    """
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)
    body = "# Heading\nthe actual ref body text"
    ref_id = await _make_ref(client, admin_token, pid, root, "Body ref", body)

    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    resp = await client.get(f"/api/public/{token}/documents/{root}/references")
    assert resp.status_code == 200, resp.text
    refs = resp.json()["references"]
    matched = [r for r in refs if r["reference_id"] == ref_id]
    assert matched, "in-scope ref must be present"
    assert matched[0].get("content") == body, (
        f"public references must carry `content` for transclusion seeding; "
        f"got content={matched[0].get('content')!r}"
    )


@pytest.mark.asyncio
async def test_public_references_hide_archived(client, admin_user, project_with_doc):
    """Archived refs are absent from the anonymous /references surface (plan
    reference-archive-v2 step 4). Parity with the default authed view — anonymous readers
    never see archived refs."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)
    live_ref = await _make_ref(client, admin_token, pid, root, "Live ref", "live body")
    archived_ref = await _make_ref(client, admin_token, pid, root, "Arch ref", "arch body")
    await client.patch(
        f"/api/references/{archived_ref}", json={"archived": True},
        cookies={"lore_session": admin_token},
    )

    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    resp = await client.get(f"/api/public/{token}/documents/{root}/references")
    assert resp.status_code == 200, resp.text
    ref_ids = {r["reference_id"] for r in resp.json()["references"]}
    assert live_ref in ref_ids
    assert archived_ref not in ref_ids


@pytest.mark.asyncio
async def test_public_references_row_carries_archived_false(
    client, admin_user, project_with_doc,
):
    """INVARIANT(anonymous-surface): `public_references` uses SELECT * + the passthrough
    serializer, so once `archived` exists on `documents` every public row serializes an
    `archived` key. The filter excludes archived rows, so the public surface only ever
    emits archived:false — benign, but it touches the anonymous surface (decision ACCEPT,
    plan reference-archive-v2 step 4). Lock it: every row is archived:false."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)
    await _make_ref(client, admin_token, pid, root, "Public ref", "body")

    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    resp = await client.get(f"/api/public/{token}/documents/{root}/references")
    assert resp.status_code == 200, resp.text
    refs = resp.json()["references"]
    assert refs, "expected at least one public ref"
    for r in refs:
        assert r.get("archived") is False, (
            f"public row must always serialize archived:false (archived rows are filtered); "
            f"got {r.get('archived')!r}"
        )


# ─── Subtree nested-root rendering + references editor parity ───────────────
#
# Two regression classes (plan "public-share-subtree-tree-and-refs-sort"):
#   1. public_tree returned a nested subtree root with its REAL (out-of-scope)
#      parent_id, which the FE buildDocumentTree orphan-drop silently discards →
#      an empty docs tab. The root's parent must be normalized to null server-side.
#   2. public_references scoped to the WHOLE subtree + no sort, while the authed
#      editor scopes to doc + ancestors and sorts depth-tier. Parity requires the
#      public surface to mirror the authed scope + sort exactly.


def _simulate_fe_build_tree(nodes):
    """Faithful replica of frontend `buildDocumentTree` orphan-drop
    (document-tree-slice.ts:32-40).

    A node is kept as a ROOT only when parent_id is None; attached as a CHILD
    only when its parent_id is present in the node set; a node whose parent_id is
    set-but-absent is SILENTLY DROPPED (this is the bug surface). Returns
    (roots, dropped_ids) so tests can assert no node vanishes and the chain
    attaches. Public tree nodes carry no is_index flag (treeNodesToDocuments sets
    is_index:false for every node), so the FE's `if (d.is_index) continue` guard
    never fires here — not modeled.
    """
    by_id = {
        n["id"]: {"id": n["id"], "parent_id": n["parent_id"], "children": []}
        for n in nodes
    }
    roots: list[dict] = []
    dropped: list[str] = []
    for n in nodes:
        node = by_id[n["id"]]
        pid = n["parent_id"]
        if pid and pid in by_id:
            by_id[pid]["children"].append(node)
        elif not pid:
            roots.append(node)
        else:
            dropped.append(n["id"])
    return roots, dropped


@pytest.mark.asyncio
async def test_public_tree_subtree_nested_root_parent_normalized_to_null(
    client, admin_user, project_with_doc,
):
    """Defect 1: a subtree shared from a NESTED root must render the whole
    subtree. The root's real parent is an out-of-scope ancestor; the FE
    `buildDocumentTree` drops any node whose parent_id is set-but-absent, so the
    un-normalized root vanished and the docs tab rendered empty.

    Fix: public_tree nulls the root node's parent_id (its DB parent is outside
    the public surface). Descendants keep their real parents (always in-subtree
    by construction of subtree_doc_ids = [root] + descendants).
    """
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    # outside → root → child → grandchild (root NESTED under an outside parent).
    outside = await _make_doc(client, admin_token, pid, "Outside", "outside body")
    root = await _make_doc(
        client, admin_token, pid, "Root", "root body", parent_id=outside,
    )
    child = await _make_doc(
        client, admin_token, pid, "Child", "child body", parent_id=root,
    )
    grandchild = await _make_doc(
        client, admin_token, pid, "Grandchild", "grandchild body", parent_id=child,
    )

    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    resp = await client.get(f"/api/public/{token}/tree")
    assert resp.status_code == 200, resp.text
    nodes = resp.json()["nodes"]
    by_id = {n["id"]: n for n in nodes}

    # outside is out-of-scope → must not appear.
    assert outside not in by_id, f"outside doc leaked into subtree tree: {set(by_id)}"
    # The shared root's parent is normalized to null (its real parent is outside
    # the public surface). Pre-fix this was `outside` → FE orphan-drop → empty.
    assert by_id[root]["parent_id"] is None, (
        f"nested subtree root parent_id must be null; "
        f"got {by_id[root]['parent_id']!r}"
    )
    # Descendants keep their real in-subtree parents.
    assert by_id[child]["parent_id"] == root
    assert by_id[grandchild]["parent_id"] == child

    # FE orphan-drop mirror: the root is the SINGLE root, nothing is dropped, and
    # the descendant chain attaches under it (the exact render the bug broke).
    roots, dropped = _simulate_fe_build_tree(nodes)
    assert dropped == [], f"FE buildDocumentTree would drop nodes: {dropped}"
    assert len(roots) == 1 and roots[0]["id"] == root, (
        f"expected single root {root}, got {[r['id'] for r in roots]}"
    )
    assert [c["id"] for c in roots[0]["children"]] == [child]
    assert [c["id"] for c in roots[0]["children"][0]["children"]] == [grandchild]


@pytest.mark.asyncio
async def test_public_references_scope_whole_subtree_excludes_outside(
    client, admin_user, project_with_doc,
):
    """public_references returns EVERY subtree ref (with content), regardless of
    which doc is viewed — public transclusion seeds inline `ref:` embeds ONLY from
    this payload (no lazy per-ref fetch on the anonymous router), and a doc may
    embed a ref owned by ANY subtree node, so the whole subtree must ship here.

    The single boundary is the share scope: a ref whose owning doc is OUTSIDE the
    subtree (doc ∉ doc_ids) must never appear — that is the real
    `resolve_share + doc ∈ doc_ids` perimeter. NOT a doc+ancestors narrowing: an
    earlier "editor parity" re-scope to doc+ancestors dropped descendant-owned
    refs and broke their embeds (see
    test_public_references_carry_descendant_ref_body_for_transclusion).
    """
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, child, grandchild, outside = await _build_tree(client, admin_token, pid)
    ref_on_root = await _make_ref(client, admin_token, pid, root, "Root ref", "r")
    ref_on_child = await _make_ref(client, admin_token, pid, child, "Child ref", "c")
    ref_on_grandchild = await _make_ref(
        client, admin_token, pid, grandchild, "Grandchild ref", "g",
    )
    ref_on_outside = await _make_ref(client, admin_token, pid, outside, "Outside ref", "o")

    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    # Viewing an ANCESTOR (root): the whole subtree's refs are present — including
    # descendant-owned (child, grandchild) — because their embeds may appear in
    # root's content and there is no anonymous lazy fetch to fill a gap.
    resp = await client.get(f"/api/public/{token}/documents/{root}/references")
    assert resp.status_code == 200, resp.text
    ref_ids = {r["reference_id"] for r in resp.json()["references"]}
    assert {ref_on_root, ref_on_child, ref_on_grandchild} <= ref_ids, ref_ids
    assert ref_on_outside not in ref_ids, (
        f"out-of-subtree ref must not appear: {ref_ids}"
    )

    # The set is independent of the viewed doc (whole subtree either way) — only
    # the sort order changes (asserted by test_public_references_sorted_depth_tier).
    resp = await client.get(f"/api/public/{token}/documents/{child}/references")
    assert resp.status_code == 200, resp.text
    ref_ids = {r["reference_id"] for r in resp.json()["references"]}
    assert {ref_on_root, ref_on_child, ref_on_grandchild} <= ref_ids, ref_ids
    assert ref_on_outside not in ref_ids, ref_ids


@pytest.mark.asyncio
async def test_public_references_sorted_depth_tier_then_sort_key(
    client, admin_user, project_with_doc,
):
    """Defect 2b parity with the authed list_references: depth-tier (own refs
    first, then ancestors by proximity), then (sort_key, id) ASC within each
    tier — manual order, not updated_at; archived rows are already filtered out
    on this surface, so only the tier+key ordering is exercised.
    """
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, child, _, _ = await _build_tree(client, admin_token, pid)

    child_ref_top = await _make_ref(client, admin_token, pid, child, "Child top", "ct")
    child_ref_bottom = await _make_ref(client, admin_token, pid, child, "Child bottom", "cb")
    root_ref = await _make_ref(client, admin_token, pid, root, "Root ref", "rr")

    # Pin deterministic keys — creation order is not the contract; the key is.
    from db import get_db
    db = await get_db()
    keys = {
        child_ref_top: "a0",
        child_ref_bottom: "c0",
        root_ref: "b0",
    }
    for rid, sk in keys.items():
        await db.query(
            "UPDATE type::record('documents', $id) SET sort_key = $sk",
            {"id": rid, "sk": sk},
        )

    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    # Viewing child: own tier (child) first in key order, then ancestor tier (root).
    resp = await client.get(f"/api/public/{token}/documents/{child}/references")
    assert resp.status_code == 200, resp.text
    order = [r["reference_id"] for r in resp.json()["references"]]
    assert order == [child_ref_top, child_ref_bottom, root_ref], (
        f"depth-tier sort (own by sort_key, then ancestors) failed: {order}"
    )


@pytest.mark.asyncio
async def test_public_references_carry_descendant_ref_body_for_transclusion(
    client, admin_user, project_with_doc,
):
    """REGRESSION (commit 17f596de): public_references must return the body of
    EVERY subtree ref, not just the current doc's ancestors.

    Public transclusion seeds inline `ref:` embeds ONLY from this response's
    payload — buildPublicTransclusionMap reads `ref.content` straight off the
    store `references`, and usePublicTransclusionSync's lazy effect SKIPS refs
    ("ref bodies are in the store already"). There is no lazy per-ref fetch on
    the anonymous router (the authed GET /references/{id} would 401), so any ref
    body absent here is unfillable. A doc may embed a ref owned by ANY subtree
    doc (e.g. a descendant), so the response must carry all subtree ref bodies.

    The doc+ancestors re-scope dropped descendant-owned refs → their embeds
    rendered permanently broken. Assert the contract: a ref owned by a descendant
    (grandchild) is present, with content, when viewing an ancestor (child).
    """
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, child, grandchild, _ = await _build_tree(client, admin_token, pid)
    body = "# G\ngrandchild ref body for transclusion"
    ref_on_grandchild = await _make_ref(
        client, admin_token, pid, grandchild, "Grandchild ref", body,
    )

    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    # Viewing child (an ancestor of grandchild): the descendant-owned ref MUST be
    # present and carry its content — child's content can embed it, and there is
    # no anonymous lazy fetch to fill a gap.
    resp = await client.get(f"/api/public/{token}/documents/{child}/references")
    assert resp.status_code == 200, resp.text
    refs = resp.json()["references"]
    matched = [r for r in refs if r["reference_id"] == ref_on_grandchild]
    assert matched, (
        f"descendant-owned ref body must be present for transclusion; "
        f"got ids {[r['reference_id'] for r in refs]}"
    )
    assert matched[0].get("content") == body, (
        f"descendant ref must carry content for inline embed; "
        f"got {matched[0].get('content')!r}"
    )


@pytest.mark.asyncio
async def test_public_file_for_in_scope_image_ref_200(
    client, admin_user, project_with_doc,
):
    """GET /api/public/{token}/files/{ref_id}/{filename} serves an in-scope image ref
    anonymously (no cookie). The image ref's owning doc must be in the share's doc_ids.

    Plan "iridescent-wibbling-heron" follow-up: parallel-render removal requires the
    anonymous surface to serve ref binaries so the shared `<ReferencesPanel>` can
    render image refs (the authed /api/files/{ref_id}/{filename} 401s anonymous).
    Reuses the same immutable Cache-Control as the authed route (INVARIANT
    files.py:42 — write-once binaries).
    """
    import io


    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)

    # Upload an image ref under the shared root via the authed upload route.
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
        b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
        b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": pid, "document_id": root, "title": "img.png"},
        files={"file": ("img.png", io.BytesIO(png_bytes), "image/png")},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    ref_id = resp.json()["reference_id"]
    file_path = resp.json()["file_path"]
    basename = file_path.split("/")[-1]

    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    # Anonymous (no lore_session cookie) — must serve the bytes with immutable cache.
    resp = await client.get(f"/api/public/{token}/files/{ref_id}/{basename}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.content == png_bytes
    assert resp.headers["cache-control"] == "private, max-age=31536000, immutable"


# ─── Anonymous thumbnails: SYSTEM: public-share + thumbnails ───────────────
#
# Mirror of test_public_file_for_in_scope_image_ref_200 / _404 for the
# /files/{reference_id}/thumb route. The ImageGallery thumb URL must not 401 on
# /s/:token — the public router exposes its own thumbnail endpoint, gated by
# the same single funnel as /files/{reference_id}/{filename}.
#
# Route-ordering invariant: the literal `/thumb` segment MUST win over the
# generic `/files/{reference_id}/{filename}` route. Asserted explicitly below
# (test_public_thumb_route_ordering_wins_over_generic_file) so a future
# refactor that reorders the routes can't silently regress to the file handler
# serving the full-size image (bandwidth) or 404ing on the missing `thumb`
# file.


_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
    b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
    b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_valid_png(width: int = 4, height: int = 4) -> bytes:
    """Generate a valid PNG via Pillow.

    WHY not reuse `_PNG_BYTES`: that literal has a broken IDAT data stream
    (see test_thumbnails.py:11). The public FILE route (test_public_file_*) just
    serves raw bytes — no PIL — so the broken stream is fine there. The public
    THUMB route runs PIL generate-on-demand, which raises on the broken stream.
    Use this helper for any thumb test.
    """
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(255, 0, 0)).save(buf, "PNG")
    return buf.getvalue()


_VALID_PNG = _make_valid_png(4, 4)


async def _upload_image_ref(client, admin_token, project_id, parent_doc, title="img.png"):
    """Upload a valid PNG image ref via the authed upload route."""
    import io
    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": project_id, "document_id": parent_doc, "title": title},
        files={"file": (title, io.BytesIO(_VALID_PNG), "image/png")},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["reference_id"]


@pytest.mark.asyncio
async def test_public_thumb_for_in_scope_image_ref_200(
    client, admin_user, project_with_doc,
):
    """GET /api/public/{token}/files/{ref_id}/thumb serves a WebP thumbnail
    anonymously (no cookie). The image ref's owning doc must be in the share's
    doc_ids — the same single funnel as the full-size file route.

    The thumb is generated on demand (write-once disk cache) — first hit runs
    PIL, subsequent hits serve the cached WebP. Content-Type MUST be image/webp
    (NOT the original image/png), confirming the thumb handler ran, not the
    generic file handler.
    """
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)
    ref_id = await _upload_image_ref(client, admin_token, pid, root)

    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    resp = await client.get(f"/api/public/{token}/files/{ref_id}/thumb")
    assert resp.status_code == 200, resp.text
    # CRITICAL: image/webp, NOT image/png — proves the thumb handler ran
    # (the generic file handler would return image/png from file_meta).
    assert resp.headers["content-type"] == "image/webp", (
        f"thumb must be WebP (thumb handler), got {resp.headers['content-type']}"
    )
    # INVARIANT files.py:42 — write-once binaries ⇒ immutable cache lifetime.
    assert resp.headers["cache-control"] == "private, max-age=31536000, immutable"


@pytest.mark.asyncio
async def test_public_thumb_for_out_of_scope_ref_404(
    client, admin_user, project_with_doc,
):
    """An image ref whose owning doc is OUT of the share's doc_ids must 404
    on the thumb route too. Single funnel (resolve_share + doc ∈ doc_ids)
    extends to thumbnails — INVARIANT public_share.py:11."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, outside = await _build_tree(client, admin_token, pid)
    ref_id = await _upload_image_ref(client, admin_token, pid, outside)

    # Share the root subtree — outside doc is NOT in doc_ids.
    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    resp = await client.get(f"/api/public/{token}/files/{ref_id}/thumb")
    assert resp.status_code == 404, (
        f"out-of-scope ref thumb must 404 via the single funnel: {resp.text}"
    )


@pytest.mark.asyncio
async def test_public_thumb_route_ordering_wins_over_generic_file(
    client, admin_user, project_with_doc,
):
    """The literal `/thumb` segment MUST resolve to the thumb handler, not the
    generic /files/{reference_id}/{filename} route.

    Route-ordering invariant: the thumb route is declared ABOVE the generic
    file route in public_share.py. If a refactor reorders them, FastAPI would
    match `thumb` as the {filename} parameter and either serve the full-size
    image (wrong Content-Type, bandwidth) or 404 on a missing `thumb` file.

    Asserted over Content-Type (image/webp) — the only way to prove the thumb
    handler ran is to check the response is WebP, since the file handler would
    return image/png.
    """
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)
    ref_id = await _upload_image_ref(client, admin_token, pid, root)

    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    # Hit /thumb — must reach the thumb handler (image/webp).
    resp = await client.get(f"/api/public/{token}/files/{ref_id}/thumb")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/webp"

    # Sanity: hitting /thumb as if it were a filename via the generic route
    # would 404 (no file named "thumb" is stored). This is the failure mode
    # the ordering invariant prevents — if the routes were flipped, the line
    # above would either 404 or serve image/png.
    from thumbnails import get_thumb_path

    from db import get_db
    db = await get_db()
    ref = await db.query(
        "SELECT project_id FROM documents WHERE meta::id(id) = $id",
        {"id": ref_id},
    )
    project_id = ref[0]["project_id"]
    thumb_disk_path = get_thumb_path(project_id, ref_id)
    # The previous hit generated it — proving the thumb handler ran.
    assert thumb_disk_path.is_file(), (
        "thumb file must exist on disk after the /thumb hit — proves the "
        "thumb handler (not the file handler) served the response"
    )


@pytest.mark.asyncio
async def test_public_thumb_requires_no_cookie(
    client, admin_user, project_with_doc,
):
    """The public thumb route must NOT depend on get_current_user — anonymous
    works (parity with every other /api/public/{token}/ route)."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)
    ref_id = await _upload_image_ref(client, admin_token, pid, root)

    data = await _mint_share(client, admin_token, pid, root, "doc")
    token = data["token"]

    # Explicitly NO lore_session cookie.
    resp = await client.get(f"/api/public/{token}/files/{ref_id}/thumb")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_public_file_for_out_of_scope_ref_404(
    client, admin_user, project_with_doc,
):
    """An image ref whose owning doc is OUT of the share's doc_ids must 404.

    The single funnel (resolve_share + doc ∈ doc_ids) extends to file serving —
    INVARIANT public_share.py:11. No existence oracle for anonymous callers.
    """
    import io

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, outside = await _build_tree(client, admin_token, pid)

    # Image ref under OUTSIDE doc.
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
        b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
        b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": pid, "document_id": outside, "title": "img.png"},
        files={"file": ("img.png", io.BytesIO(png_bytes), "image/png")},
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    ref_id = resp.json()["reference_id"]
    file_path = resp.json()["file_path"]
    basename = file_path.split("/")[-1]

    # Share the root subtree — outside doc is NOT in doc_ids.
    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    resp = await client.get(f"/api/public/{token}/files/{ref_id}/{basename}")
    assert resp.status_code == 404, (
        f"out-of-scope ref file must 404 via the single funnel: {resp.text}"
    )


@pytest.mark.asyncio
async def test_public_file_path_traversal_rejected(
    client, admin_user, project_with_doc,
):
    """A ref whose stored file_path escapes STORAGE_PATH must be rejected, not leak
    bytes.

    The shared resolve_reference_file helper enforces `abs_path.is_relative_to
    (STORAGE_PATH)` and returns a uniform 404 (plan tool-surface-consolidation
    Step 4: the security chain has one home; a traversal is treated like a
    not-found — no byte leak, no 200/500). 404 (not 403) is the uniform posture:
    it reveals nothing about whether the id/file exists.
    """
    from config import STORAGE_PATH
    from db import get_db

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)

    # Make a markdown ref under root, then poison its file_path with a traversal.
    ref_id = await _make_ref(client, admin_token, pid, root, "Poisoned")
    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET "
        "media_type = 'image', file_path = '../../../etc/passwd'",
        {"id": ref_id},
    )

    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    resp = await client.get(f"/api/public/{token}/files/{ref_id}/passwd")
    assert resp.status_code == 404, (
        f"path-traversal must be rejected (not 200/500): {resp.status_code} {resp.text}"
    )
    # Sanity: STORAGE_PATH is unambiguous and the poisoned path escapes it.
    assert STORAGE_PATH


@pytest.mark.asyncio
async def test_public_notes_for_in_scope_docs(client, admin_user, project_with_doc):
    """REMOVED: notes are no longer on the public surface.

    Plan "public-share-reuse-readonly-layout" drops the notes endpoint and its
    FE panel from /s/:token (readonly role + scope creep). The endpoint is gone;
    the assertion below guards against a regression that re-introduces it.
    """
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)

    data = await _mint_share(client, admin_token, pid, root, "doc")
    token = data["token"]

    resp = await client.get(f"/api/public/{token}/documents/{root}/notes")
    assert resp.status_code == 404, (
        f"public /notes endpoint must remain removed: {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_public_document_404_after_project_delete(client, admin_user, project_with_doc):
    """A live share row on a DELETED project serves nothing: project delete
    marks only the project row (the share and the document survive), so
    resolve_share must check project liveness itself — uniform 404, no
    existence oracle for anonymous callers."""
    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user

    await _mint_share(client, admin_token, pid, idx_id, "doc")
    # False-green check: the share serves BEFORE the delete.
    resp = await client.get(f"/api/public/documents/{idx_id}")
    assert resp.status_code == 200, resp.text

    resp = await client.delete(f"/api/projects/{pid}", cookies={"lore_session": admin_token})
    assert resp.status_code == 200, resp.text

    resp = await client.get(f"/api/public/documents/{idx_id}")
    assert resp.status_code == 404


# ─── REFUSED principal — the security surface is the whole story ──────────


@pytest.mark.asyncio
async def test_refused_out_of_scope_document_404(client, admin_user, project_with_doc):
    """A doc-A token must 404 on doc B (content/refs)."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, child, grandchild, outside = await _build_tree(client, admin_token, pid)

    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    # Content
    resp = await client.get(f"/api/public/{token}/documents/{outside}")
    assert resp.status_code == 404, f"out-of-scope doc must 404 (content): {resp.text}"
    # References
    resp = await client.get(f"/api/public/{token}/documents/{outside}/references")
    assert resp.status_code == 404, f"out-of-scope doc must 404 (refs): {resp.text}"


@pytest.mark.asyncio
async def test_refused_write_verbs_not_routed(client, admin_user, project_with_doc):
    """INVARIANT: no write/upload/WS endpoints on the public router — by construction."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)
    data = await _mint_share(client, admin_token, pid, root, "doc")
    token = data["token"]

    for method in ("post", "patch", "delete", "put"):
        resp = await getattr(client, method)(f"/api/public/{token}/tree")
        assert resp.status_code in (404, 405), (
            f"{method} must NOT be routed on the public surface: {resp.status_code}"
        )


@pytest.mark.asyncio
async def test_refused_deleted_share_404(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)
    data = await _mint_share(client, admin_token, pid, root, "doc")
    token = data["token"]

    # Pre-condition: the share works.
    assert (await client.get(f"/api/public/{token}/tree")).status_code == 200

    await client.delete(
        f"/api/shares/{data['share_id']}",
        cookies={"lore_session": admin_token},
    )

    # Post-condition: revoked → 404.
    resp = await client.get(f"/api/public/{token}/tree")
    assert resp.status_code == 404, f"revoked share must 404: {resp.text}"


@pytest.mark.asyncio
async def test_refused_invalid_token_404(client, admin_user, project_with_doc):
    resp = await client.get("/api/public/lore_nope_this_is_not_a_real_token/tree")
    assert resp.status_code == 404, f"invalid token must 404: {resp.text}"


@pytest.mark.asyncio
async def test_refused_public_endpoints_require_no_cookie(client, admin_user, project_with_doc):
    """The public router must NOT depend on get_current_user — anonymous works."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)
    data = await _mint_share(client, admin_token, pid, root, "doc")
    token = data["token"]

    # Explicitly NO lore_session cookie.
    resp = await client.get(f"/api/public/{token}/tree")
    assert resp.status_code == 200, resp.text


# ─── Live semantics: move makes a doc public ───────────────────────────────


@pytest.mark.asyncio
async def test_doc_moved_under_shared_root_becomes_readable(
    client, admin_user, project_with_doc,
):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)
    # Doc born OUTSIDE the subtree.
    outsider = await _make_doc(client, admin_token, pid, "Outsider", "outsider body")

    data = await _mint_share(client, admin_token, pid, root, "subtree")
    token = data["token"]

    # Pre-move: outsider is NOT readable.
    resp = await client.get(f"/api/public/{token}/documents/{outsider}")
    assert resp.status_code == 404

    # Move outsider under root via the documents API.
    from db import get_db
    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET parent_id = $pid",
        {"id": outsider, "pid": root},
    )

    # Post-move: outsider IS readable. The plan accepts a short TTL on the
    # doc_ids cache (moves reflect after TTL); we clear the cache here to test
    # the underlying live semantics without coupling to the TTL duration.
    import routes.public_share as public_share
    public_share._doc_ids_cache.clear()

    resp = await client.get(f"/api/public/{token}/documents/{outsider}")
    assert resp.status_code == 200, (
        f"doc moved under shared root must become readable: {resp.text}"
    )


# ─── Depth-50 descendant cap (contract assertion) ──────────────────────────


@pytest.mark.asyncio
async def test_subtree_doc_ids_depth_cap_50(client, admin_user, project_with_doc):
    """db.get_descendant_ids is depth-capped at 50; a 52-deep chain loses the tail.

    Asserts over the DERIVED helper (subtree_doc_ids) — the public-share subtree
    scope is contractually bounded by this cap. Documenting it here makes a
    regression visible.
    """
    pid, _, _ = project_with_doc
    _, admin_token = admin_user

    # Build a 52-deep linear chain directly via DB (fast; avoids 52 API calls).
    from db import create_record
    chain_root = "depth-root"
    await create_record("documents", chain_root, {
        "project_id": pid, "parent_id": None, "title": "d0",
        "content": "", "path": "d0.md", "is_index": False,
    })
    prev = chain_root
    deepest = chain_root
    for i in range(1, 53):
        deepest = f"depth-{i}"
        await create_record("documents", deepest, {
            "project_id": pid, "parent_id": prev, "title": f"d{i}",
            "content": "", "path": f"d{i}.md", "is_index": False,
        })
        prev = deepest

    from scope import subtree_doc_ids
    ids = await subtree_doc_ids(chain_root, pid)

    # The root + 50 levels of descendants = 51 ids; the 52nd level is dropped.
    assert chain_root in ids
    assert len(ids) <= 51, f"depth cap failed: got {len(ids)} ids"
    assert deepest not in ids, (
        f"the 52nd level ({deepest}) must be truncated by the depth-50 cap; got {len(ids)} ids"
    )


# ─── doc_ids cache eviction (T3: memory-leak fix) ───────────────────────────


@pytest.mark.asyncio
async def test_doc_ids_cache_prunes_expired_on_write():
    """T3: a token accessed once must not live forever. The TTL governs freshness
    only; without prune-on-write the cache grows with every distinct token ever
    seen. Before inserting a fresh entry, expired entries are dropped, bounding
    the cache to the active token set."""
    import time as _time

    import routes.public_share as ps

    ps._doc_ids_cache.clear()
    now = _time.monotonic()
    ps._doc_ids_cache["stale1"] = (["x"], now - 5)   # expired
    ps._doc_ids_cache["stale2"] = (["y"], now - 1)   # expired
    ps._doc_ids_cache["fresh"] = (["z"], now + 5)    # still valid

    with patch("routes.public_share.subtree_doc_ids", new_callable=AsyncMock) as mock_sub:
        mock_sub.return_value = ["new"]
        result = await ps._resolve_doc_ids("proj", "root", "subtree", "newtok")

    assert result == ["new"]
    assert "newtok" in ps._doc_ids_cache
    assert "fresh" in ps._doc_ids_cache          # not expired → kept
    assert "stale1" not in ps._doc_ids_cache     # expired → pruned
    assert "stale2" not in ps._doc_ids_cache     # expired → pruned
    ps._doc_ids_cache.clear()


@pytest.mark.asyncio
async def test_doc_ids_cache_hard_cap_drops_oldest():
    """T3 hard guard: even with no expiry, a pathological churn of distinct fresh
    tokens must not grow the cache without bound. Above the cap the oldest (lowest
    expiry) entry is dropped. Entries share a uniform TTL, so the just-inserted
    token (expiry now+TTL) is newest and survives; the stalest is evicted."""
    import time as _time

    import routes.public_share as ps

    ps._doc_ids_cache.clear()
    cap = ps._DOC_IDS_CACHE_CAP
    base = _time.monotonic()
    # Seed fresh entries whose expiries all sit below the real TTL window so the
    # just-inserted entry (expiry now+TTL) is the newest, as in production.
    step = (ps._DOC_IDS_TTL_S - 1.0) / cap
    for i in range(cap):
        ps._doc_ids_cache[f"t{i}"] = ([f"d{i}"], base + 1 + i * step)

    with patch("routes.public_share.subtree_doc_ids", new_callable=AsyncMock) as mock_sub:
        mock_sub.return_value = ["overflow"]
        await ps._resolve_doc_ids("proj", "root", "subtree", "overflow")

    assert len(ps._doc_ids_cache) <= cap
    assert "overflow" in ps._doc_ids_cache
    assert "t0" not in ps._doc_ids_cache          # lowest expiry → dropped first
    ps._doc_ids_cache.clear()


# ─── Re-keyed funnel: resolve by document_id (plan "public-document-ids") ────
#
# The share token stops being the URL segment; resolve_share is keyed on the
# document's own uuid. A doc is published iff it has a live document_shares row
# OR an ancestor has one with scope='subtree'. The agent-config subtree is
# unshareable by ANCESTRY (a is_system=false leaf under the system root must
# still 404 — the flag-only check would pass it). The {token} endpoints remain
# as aliases (covered by the suite above); these tests target the document_id
# key and the new /api/public/documents/{document_id}/* surface.


async def _insert_share_row(project_id: str, document_id: str, scope: str = "doc") -> str:
    """Insert a document_shares row directly (bypass the mint HTTP endpoint).

    Used to simulate a share row that predates the ancestry guard (the Risks
    scenario: a row minted before the guard MUST stop resolving), and to exercise
    resolve_share without coupling to the mint path.
    """
    from uuid import uuid4

    from db import create_record
    sid = str(uuid4())
    await create_record("document_shares", sid, {
        "project_id": project_id,
        "document_id": document_id,
        "scope": scope,
        "token": "lore_test_" + uuid4().hex,
        "created_by": None,
    })
    return sid


@pytest.mark.asyncio
async def test_resolve_share_rekeyed_on_document_id_direct_row(client, admin_user, project_with_doc):
    """resolve_share(document_id=...) finds a direct row on that doc.

    Red signal: today resolve_share is keyed on `token` (positional), so calling
    it with document_id= raises TypeError — the right-reason failure until the
    funnel is re-keyed.
    """
    from routes.public_share import resolve_share
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)
    await _insert_share_row(pid, root, "doc")

    ctx = await resolve_share(document_id=root)
    assert root in ctx["doc_ids"]
    assert ctx["scope"] == "doc"


@pytest.mark.asyncio
async def test_resolve_share_finds_subtree_ancestor(client, admin_user, project_with_doc):
    """A doc with NO direct row is published when an ancestor has scope='subtree'."""
    from routes.public_share import resolve_share
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, child, grandchild, _ = await _build_tree(client, admin_token, pid)
    await _insert_share_row(pid, root, "subtree")

    ctx = await resolve_share(document_id=grandchild)
    assert grandchild in ctx["doc_ids"], "descendant readable via ancestor subtree share"
    assert child in ctx["doc_ids"]


@pytest.mark.asyncio
async def test_resolve_share_unpublished_doc_404(client, admin_user, project_with_doc):
    """A doc with neither a direct row nor a subtree ancestor is unpublished → 404.

    Positive anchor first (the minted doc resolves), so the test fails today on
    the not-yet-re-keyed funnel rather than passing trivially.
    """
    from fastapi import HTTPException
    from routes.public_share import resolve_share
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, child, _, outside = await _build_tree(client, admin_token, pid)
    await _insert_share_row(pid, child, "doc")  # only child is published

    ctx = await resolve_share(document_id=child)  # positive anchor
    assert child in ctx["doc_ids"]

    with pytest.raises(HTTPException) as ei:
        await resolve_share(document_id=outside)  # no row, no subtree ancestor
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_resolve_share_refuses_system_root_descendant_leaf(
    client, admin_user, project_with_doc,
):
    """Risks: a is_system=false LEAF under the system root must NOT resolve, even
    with a pre-existing share row. The guard is by ANCESTRY, not the flag — a
    flag-only check would pass this leaf (is_system=false). Tests the RESOLVE path
    (a row minted before the guard must stop resolving). Positive anchor: a normal
    doc with a row resolves fine.
    """
    from agent_config import ensure_agent_system_docs
    from fastapi import HTTPException
    from routes.public_share import resolve_share
    pid, _, _ = project_with_doc
    _, admin_token = admin_user

    roles = await ensure_agent_system_docs(pid)
    system_root_id = roles["system_root"]
    # A plain user doc nested under the system root (is_system=false, no role tag).
    leaf = await _make_doc(
        client, admin_token, pid, "Leaf under system root",
        "secret agent memory", parent_id=system_root_id,
    )
    await _insert_share_row(pid, leaf, "doc")

    # Positive anchor: a normal doc with a row resolves.
    normal = await _make_doc(client, admin_token, pid, "Normal doc", "public")
    await _insert_share_row(pid, normal, "doc")
    ctx_normal = await resolve_share(document_id=normal)
    assert normal in ctx_normal["doc_ids"]

    # The system-root leaf must NOT resolve despite the row.
    with pytest.raises(HTTPException) as ei:
        await resolve_share(document_id=leaf)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_resolve_share_refuses_system_root_itself(client, admin_user, project_with_doc):
    """The system root itself is unshareable, even with a pre-existing subtree row."""
    from agent_config import ensure_agent_system_docs
    from agent_config_seed import _deterministic_id
    from fastapi import HTTPException
    from routes.public_share import resolve_share
    pid, _, _ = project_with_doc
    await ensure_agent_system_docs(pid)
    system_root_id = _deterministic_id(pid, "system_root")
    await _insert_share_row(pid, system_root_id, "subtree")

    with pytest.raises(HTTPException) as ei:
        await resolve_share(document_id=system_root_id)
    assert ei.value.status_code == 404


# ─── /api/public/documents/{document_id}/* HTTP endpoints ────────────────────


@pytest.mark.asyncio
async def test_public_documents_endpoint_doc_scope_root_only(
    client, admin_user, project_with_doc,
):
    """GET /api/public/documents/{id}/tree returns the root only for doc scope."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, _, _, _ = await _build_tree(client, admin_token, pid)
    await _insert_share_row(pid, root, "doc")

    resp = await client.get(f"/api/public/documents/{root}/tree")
    assert resp.status_code == 200, resp.text
    ids = {n["id"] for n in resp.json()["nodes"]}
    assert ids == {root}


@pytest.mark.asyncio
async def test_public_documents_endpoint_subtree_descendant_readable(
    client, admin_user, project_with_doc,
):
    """A descendant is readable at its OWN document_id under a subtree share."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, child, grandchild, _ = await _build_tree(client, admin_token, pid)
    await _insert_share_row(pid, root, "subtree")

    resp = await client.get(f"/api/public/documents/{grandchild}/tree")
    assert resp.status_code == 200, resp.text
    ids = {n["id"] for n in resp.json()["nodes"]}
    assert grandchild in ids


@pytest.mark.asyncio
async def test_public_documents_endpoint_sibling_and_parent_of_doc_scope_404(
    client, admin_user, project_with_doc,
):
    """Risks negative: sibling and parent of a scope='doc' share must 404.

    Positive anchor (the minted child IS readable) makes the test fail today on
    the absent endpoint, not pass trivially on a missing route.
    """
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, child, _, outside = await _build_tree(client, admin_token, pid)
    await _insert_share_row(pid, child, "doc")

    resp_child = await client.get(f"/api/public/documents/{child}/tree")
    assert resp_child.status_code == 200, resp_child.text

    resp_sib = await client.get(f"/api/public/documents/{outside}/tree")
    assert resp_sib.status_code == 404
    resp_parent = await client.get(f"/api/public/documents/{root}/tree")
    assert resp_parent.status_code == 404


@pytest.mark.asyncio
async def test_public_documents_endpoint_unknown_id_404(client, project_with_doc):
    """Unknown id → uniform 404 (no existence oracle)."""
    from uuid import uuid4
    resp = await client.get(f"/api/public/documents/{uuid4()}/tree")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_public_documents_endpoint_system_leaf_404(client, admin_user, project_with_doc):
    """HTTP-level: a system-root descendant leaf with a pre-existing row 404s.

    Positive anchor (a normal doc with a row is readable) makes the test fail
    today on the absent endpoint, not pass trivially on a missing route.
    """
    from agent_config import ensure_agent_system_docs
    pid, _, _ = project_with_doc
    _, admin_token = admin_user

    # Positive anchor: a normal doc with a row resolves over HTTP.
    normal = await _make_doc(client, admin_token, pid, "Normal", "public")
    await _insert_share_row(pid, normal, "doc")
    resp_normal = await client.get(f"/api/public/documents/{normal}/tree")
    assert resp_normal.status_code == 200, resp_normal.text

    roles = await ensure_agent_system_docs(pid)
    leaf = await _make_doc(
        client, admin_token, pid, "secret", "x", parent_id=roles["system_root"],
    )
    await _insert_share_row(pid, leaf, "doc")

    resp = await client.get(f"/api/public/documents/{leaf}/tree")
    assert resp.status_code == 404


# ─── Step 2: mint guard — refuse the system subtree at the chokepoint ────────
#
# The ancestry guard runs at BOTH the resolve funnel (Step 1) AND the mint
# chokepoint (here). A row minted before the guard is still caught at resolve;
# the mint guard prevents creating such rows going forward and returns the
# canonical /docs/<document_id> URL on success.


@pytest.mark.asyncio
async def test_mint_refuses_system_root_descendant_400(client, admin_user, project_with_doc):
    """Minting on a system-root descendant leaf → 400 (ancestry, not the flag).

    A is_system=false leaf under the system root is refused — the flag-only
    check would pass it. Positive anchor: a normal doc mints fine.
    """
    from agent_config import ensure_agent_system_docs

    pid, _, _ = project_with_doc
    _, admin_token = admin_user

    roles = await ensure_agent_system_docs(pid)
    leaf = await _make_doc(
        client, admin_token, pid, "under system root", "x",
        parent_id=roles["system_root"],
    )

    # Positive anchor: a normal doc mints fine.
    normal = await _make_doc(client, admin_token, pid, "normal", "y")
    resp_ok = await client.post(
        f"/api/projects/{pid}/documents/{normal}/shares",
        json={"scope": "doc"}, cookies={"lore_session": admin_token},
    )
    assert resp_ok.status_code == 200, resp_ok.text

    # System-root descendant leaf must be refused at the mint chokepoint.
    resp = await client.post(
        f"/api/projects/{pid}/documents/{leaf}/shares",
        json={"scope": "doc"}, cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 400, f"system-root descendant must 400: {resp.text}"


@pytest.mark.asyncio
async def test_mint_refuses_system_root_itself_400(client, admin_user, project_with_doc):
    """Minting on the system root itself → 400."""
    from agent_config import ensure_agent_system_docs
    from agent_config_seed import _deterministic_id

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    await ensure_agent_system_docs(pid)
    sys_root = _deterministic_id(pid, "system_root")

    resp = await client.post(
        f"/api/projects/{pid}/documents/{sys_root}/shares",
        json={"scope": "subtree"}, cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 400, f"system root must 400: {resp.text}"


@pytest.mark.asyncio
async def test_mint_response_returns_canonical_doc_url(client, admin_user, project_with_doc):
    """Mint response carries the canonical /docs/<document_id> URL."""
    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user
    resp = await client.post(
        f"/api/projects/{pid}/documents/{idx_id}/shares",
        json={"scope": "doc"}, cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("url") == f"/docs/{idx_id}", (
        f"mint response must carry canonical /docs/<id> url, got: {body}"
    )


# ─── PATCH scope + inherited_from (plan: General access as two checkboxes) ─────
#
# Scope change is a new atomic PATCH (not revoke-then-create), and LIST now reports
# `inherited_from` so the Access summary can say "Published via a parent folder"
# instead of falsely "Not shared" for a doc covered by an ancestor's subtree share.


async def _patch_share_scope(client, token: str, share_id: str, scope: str):
    return await client.patch(
        f"/api/shares/{share_id}",
        json={"scope": scope},
        cookies={"lore_session": token},
    )


@pytest.mark.asyncio
async def test_owner_patches_share_scope_doc_to_subtree(client, admin_user, project_with_doc):
    """PATCH atomically widens scope; the row keeps its id/created_at/created_by
    audit trail and the canonical /docs/<id> URL (so the link people hold never
    changes on a scope widening)."""
    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user
    minted = await _mint_share(client, admin_token, pid, idx_id, "doc")

    resp = await _patch_share_scope(client, admin_token, minted["share_id"], "subtree")
    assert resp.status_code == 200, resp.text

    # Same row (id preserved), scope now subtree, audit fields untouched.
    from db import get_db
    db = await get_db()
    rows = await db.query(
        "SELECT * FROM document_shares WHERE id = type::record('document_shares', $id)",
        {"id": minted["share_id"]},
    )
    row = rows[0]
    assert row["scope"] == "subtree"
    assert row["token"] == minted["token"], "PATCH must NOT rotate the token"
    assert row["created_by"] is not None, "created_by audit trail preserved"


@pytest.mark.asyncio
async def test_owner_patches_share_scope_subtree_to_doc(client, admin_user, project_with_doc):
    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user
    minted = await _mint_share(client, admin_token, pid, idx_id, "subtree")
    resp = await _patch_share_scope(client, admin_token, minted["share_id"], "doc")
    assert resp.status_code == 200, resp.text
    from db import get_db
    db = await get_db()
    rows = await db.query(
        "SELECT scope FROM document_shares WHERE id = type::record('document_shares', $id)",
        {"id": minted["share_id"]},
    )
    assert rows[0]["scope"] == "doc"


@pytest.mark.asyncio
async def test_non_owner_cannot_patch_share_scope(client, admin_user, regular_user, project_with_doc):
    """INVARIANT: scope writes stay owner-only — the patch is a write affordance."""
    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user
    user_uid, user_token = regular_user
    await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": user_uid, "access_level": "full"},
        cookies={"lore_session": admin_token},
    )
    minted = await _mint_share(client, admin_token, pid, idx_id, "doc")
    resp = await _patch_share_scope(client, user_token, minted["share_id"], "subtree")
    assert resp.status_code == 403, f"non-owner full must NOT patch scope: {resp.text}"


@pytest.mark.asyncio
async def test_patch_unknown_share_404(client, admin_user, project_with_doc):
    _, admin_token = admin_user
    resp = await _patch_share_scope(client, admin_token, "00000000-0000-0000-0000-000000000000", "subtree")
    assert resp.status_code == 404, f"unknown share id must 404: {resp.text}"


@pytest.mark.asyncio
async def test_patch_invalid_scope_400(client, admin_user, project_with_doc):
    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user
    minted = await _mint_share(client, admin_token, pid, idx_id, "doc")
    resp = await _patch_share_scope(client, admin_token, minted["share_id"], "bogus")
    assert resp.status_code == 400, f"invalid scope must 400: {resp.text}"


@pytest.mark.asyncio
async def test_patch_already_deleted_share_404(client, admin_user, project_with_doc):
    """A soft-deleted row is indistinguishable from missing (uniform 404)."""
    pid, idx_id, _ = project_with_doc
    _, admin_token = admin_user
    minted = await _mint_share(client, admin_token, pid, idx_id, "doc")
    await client.delete(f"/api/shares/{minted['share_id']}", cookies={"lore_session": admin_token})
    resp = await _patch_share_scope(client, admin_token, minted["share_id"], "subtree")
    assert resp.status_code == 404, f"deleted share must 404: {resp.text}"


@pytest.mark.asyncio
async def test_list_shares_reports_inherited_from_subtree_ancestor(client, admin_user, project_with_doc):
    """INVARIANT(security): the Access summary must NOT say "Not shared" for a doc that
    resolves to a live share. A subtree share on an ancestor publishes this document
    (_find_share_row nearest-first walk), so LIST reports `inherited_from`."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, child, _, _ = await _build_tree(client, admin_token, pid)
    await _mint_share(client, admin_token, pid, root, "subtree")

    resp = await client.get(
        f"/api/projects/{pid}/documents/{child}/shares",
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # No OWN row on child → shares empty, but coverage is inherited from root.
    assert body["shares"] == []
    assert body["inherited_from"] == {"document_id": root, "title": "Root"}, (
        f"subtree ancestor must surface as inherited_from; got {body['inherited_from']}"
    )


@pytest.mark.asyncio
async def test_list_shares_no_inherited_from_doc_scope_ancestor(client, admin_user, project_with_doc):
    """A doc-scope ancestor publishes ONLY itself, so a descendant is NOT covered —
    inherited_from stays null (not a false 'inherited' report)."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, child, _, _ = await _build_tree(client, admin_token, pid)
    await _mint_share(client, admin_token, pid, root, "doc")

    resp = await client.get(
        f"/api/projects/{pid}/documents/{child}/shares",
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["shares"] == []
    assert body["inherited_from"] is None


@pytest.mark.asyncio
async def test_list_shares_own_row_wins_over_inherited(client, admin_user, project_with_doc):
    """An own row (any scope) takes precedence over an ancestor — inherited_from is
    null when the document has its own row, even if an ancestor also covers it."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root, child, _, _ = await _build_tree(client, admin_token, pid)
    # Ancestor subtree share AND the doc's own row both exist.
    await _mint_share(client, admin_token, pid, root, "subtree")
    await _mint_share(client, admin_token, pid, child, "doc")

    resp = await client.get(
        f"/api/projects/{pid}/documents/{child}/shares",
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["shares"]) == 1, "own row is listed"
    assert body["inherited_from"] is None, "own row wins → not inherited"


@pytest.mark.asyncio
async def test_list_shares_no_share_inherited_from_null(client, admin_user, project_with_doc):
    """A document with no covering share anywhere reports null + empty shares."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    _, _, grandchild, _ = await _build_tree(client, admin_token, pid)

    resp = await client.get(
        f"/api/projects/{pid}/documents/{grandchild}/shares",
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["shares"] == []
    assert body["inherited_from"] is None


@pytest.mark.asyncio
async def test_list_shares_inherited_from_nearest_subtree_ancestor(client, admin_user, project_with_doc):
    """When several ancestors carry subtree shares, the NEAREST one wins
    (_find_share_row is nearest-first) — the summary names that folder, not a
    higher one."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root = await _make_doc(client, admin_token, pid, "Root", "r")
    mid = await _make_doc(client, admin_token, pid, "Mid", "m", parent_id=root)
    leaf = await _make_doc(client, admin_token, pid, "Leaf", "l", parent_id=mid)
    await _mint_share(client, admin_token, pid, root, "subtree")
    await _mint_share(client, admin_token, pid, mid, "subtree")

    resp = await client.get(
        f"/api/projects/{pid}/documents/{leaf}/shares",
        cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["inherited_from"] == {"document_id": mid, "title": "Mid"}, (
        "nearest subtree ancestor must win"
    )

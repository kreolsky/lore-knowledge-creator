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
@pytest.mark.parametrize("case", ["foreign_project", "deleted"])
async def test_owner_cannot_mint_for_a_doc_outside_the_project(
    client, admin_user, project_with_doc, case,
):
    """The owner of THIS project gets a uniform 404 for a doc of another project
    or a deleted doc of its own, and no share row is written."""
    from db import create_record, get_db

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc_id = f"share-outside-{case}"
    await create_record("documents", doc_id, {
        "project_id": "some-other-project" if case == "foreign_project" else pid,
        "parent_id": None, "title": "Outside", "content": "x", "is_index": False,
        "path": f"/{doc_id}",
    })
    db = await get_db()
    if case == "deleted":
        await db.query(
            "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
            {"id": doc_id},
        )

    resp = await client.post(
        f"/api/projects/{pid}/documents/{doc_id}/shares",
        json={"scope": "doc"}, cookies={"lore_session": admin_token},
    )

    assert resp.status_code == 404, resp.text
    rows = await db.query(
        "SELECT * FROM document_shares WHERE document_id = $did", {"did": doc_id},
    )
    assert rows == []


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


# ─── Anonymous reads: SYSTEM: public-share ─────────────────────────────────


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
async def test_refused_invalid_token_404(client, admin_user, project_with_doc):
    resp = await client.get("/api/public/lore_nope_this_is_not_a_real_token/tree")
    assert resp.status_code == 404, f"invalid token must 404: {resp.text}"


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


# ─── Step 2: mint guard — refuse the system subtree at the chokepoint ────────
#
# The ancestry guard runs at BOTH the resolve funnel (Step 1) AND the mint
# chokepoint (here). A row minted before the guard is still caught at resolve;
# the mint guard prevents creating such rows going forward and returns the
# canonical /docs/<document_id> URL on success.


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

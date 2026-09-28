"""Project wall on by-id readers — backlinks + export inlining.

A by-id reader (GET /documents/{id}/backlinks, export transclusion inlining)
stops at the READ document's project: doc_mentions edges and doc:/ref: embeds
survive cross-project subtree moves (documents/move.py never rewrites them),
so without a wall a member of the target project would learn titles,
timestamps and BODIES of documents in a project he cannot read. The wall is
project equality (not per-target access resolution — positioning: small
trusted group; per-target resolution would cost N×4 round-trips on the export
path, audit N4).
"""

import pytest


async def _mk_project(client, token, name):
    r = await client.post("/api/projects", json={"name": name}, cookies={"lore_session": token})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["project_id"], body["index_doc_id"]


async def _mk_doc(client, pid, token, *, title, parent_id=None, content=""):
    body = {"project_id": pid, "title": title, "parent_id": parent_id, "content": content}
    r = await client.post("/api/documents", json=body, cookies={"lore_session": token})
    assert r.status_code == 200, r.text
    return r.json()["document_id"]


async def _add_member(client, admin_token, pid, uid, level="readonly"):
    r = await client.post(
        f"/api/admin/projects/{pid}/members",
        json={"user_id": uid, "access_level": level},
        cookies={"lore_session": admin_token},
    )
    assert r.status_code == 200, r.text


async def _move(client, token, doc_id, target_pid, parent_id=None):
    return await client.post(
        f"/api/documents/{doc_id}/move",
        json={"target_project_id": target_pid, "parent_id": parent_id},
        cookies={"lore_session": token},
    )


async def _export_md(client, token, doc_id):
    r = await client.get(
        f"/api/documents/{doc_id}/export", params={"format": "md"},
        cookies={"lore_session": token},
    )
    assert r.status_code == 200, r.text
    return r.text


# ─── (a) backlinks: foreign sources hidden, same-project sources kept ─────────


@pytest.mark.asyncio
async def test_backlinks_of_moved_doc_hide_foreign_sources(
    client, admin_user, regular_user,
):
    """A-doc links to D; D's subtree moves to B. A B-only member's backlinks
    of D must exclude the A-doc (the doc_mentions edge survived the move);
    the A-owner's backlinks of an A-doc still include same-project sources."""
    admin_uid, admin_token = admin_user
    user_uid, user_token = regular_user

    pid_a, idx_a = await _mk_project(client, admin_token, "Wall Backlinks A")
    pid_b, _ = await _mk_project(client, admin_token, "Wall Backlinks B")
    await _add_member(client, admin_token, pid_b, user_uid)

    a1 = await _mk_doc(client, pid_a, admin_token, title="A1 source", parent_id=idx_a)
    d = await _mk_doc(client, pid_a, admin_token, title="Movable D", parent_id=idx_a)
    # a1 links to d → doc_mentions edge a1→d (REST fallback path rebuilds it).
    r = await client.patch(
        f"/api/documents/{a1}", json={"content": f"see [D](doc:{d})"},
        cookies={"lore_session": admin_token},
    )
    assert r.status_code == 200, r.text

    resp = await _move(client, admin_token, d, pid_b)
    assert resp.status_code == 200, resp.text

    # B-only member: the A-doc source must not appear (no title/timestamp leak).
    r = await client.get(f"/api/documents/{d}/backlinks", cookies={"lore_session": user_token})
    assert r.status_code == 200, r.text
    assert r.json()["backlinks"] == [], \
        "a B-only member must not see A-project sources on a moved doc"

    # A-owner: same-project sources still resolve.
    a2 = await _mk_doc(client, pid_a, admin_token, title="A2 linker", parent_id=idx_a)
    r = await client.patch(
        f"/api/documents/{a2}", json={"content": f"see [A1](doc:{a1})"},
        cookies={"lore_session": admin_token},
    )
    assert r.status_code == 200, r.text
    r = await client.get(f"/api/documents/{a1}/backlinks", cookies={"lore_session": admin_token})
    assert r.status_code == 200, r.text
    ids = [b["document_id"] for b in r.json()["backlinks"]]
    assert a2 in ids, "same-project sources must still be returned"


# ─── (b) export: foreign embeds skipped, same-project embeds still inline ─────


@pytest.mark.asyncio
async def test_export_of_moved_doc_skips_foreign_embeds(
    client, test_db, admin_user, regular_user,
):
    """D (moved to B) embeds `![x](doc:A1)` and `![i](ref:A-image)` from A plus
    `![y](doc:B1)` from B. A B-only member's markdown export of D contains
    neither A1's body nor the image base64; the same-project embed inlines."""
    from config import STORAGE_PATH
    from db import create_record

    _, admin_token = admin_user
    user_uid, user_token = regular_user

    pid_a, idx_a = await _mk_project(client, admin_token, "Wall Export A")
    pid_b, idx_b = await _mk_project(client, admin_token, "Wall Export B")
    await _add_member(client, admin_token, pid_b, user_uid)

    b1 = await _mk_doc(
        client, pid_b, admin_token, title="B1 visible", parent_id=idx_b,
        content="B1 VISIBLE BODY",
    )
    a1 = await _mk_doc(
        client, pid_a, admin_token, title="A1 secret", parent_id=idx_a,
        content="A1 SECRET BODY",
    )

    # An A-project image reference with real bytes on disk (base64 would fire).
    img_id = "xwall-img-001"
    img_rel = "xwall/img-001.png"
    (STORAGE_PATH / "xwall").mkdir(parents=True, exist_ok=True)
    (STORAGE_PATH / img_rel).write_bytes(b"\x89PNG\r\n\x1a\nWALLIMG")
    await create_record("documents", img_id, {
        "project_id": pid_a, "parent_id": a1, "title": "Foreign image",
        "content": "", "path": f"_ref/{img_id}.md", "is_index": False,
        "is_reference": True, "media_type": "image",
        "file_path": img_rel, "file_meta": {"mime_type": "image/png"},
    })

    d = await _mk_doc(
        client, pid_a, admin_token, title="Movable with embeds", parent_id=idx_a,
        content=f"![x](doc:{a1})\n![i](ref:{img_id})\n![y](doc:{b1})",
    )

    resp = await _move(client, admin_token, d, pid_b)
    assert resp.status_code == 200, resp.text

    body = await _export_md(client, user_token, d)
    assert "A1 SECRET BODY" not in body, "foreign doc body must not inline"
    assert "base64" not in body, "foreign image bytes must not inline"
    # Skipped ≠ dropped: a skipped target stays verbatim (same shape as an
    # unresolvable one — no existence oracle on the embed syntax).
    assert f"![x](doc:{a1})" in body
    assert f"![i](ref:{img_id})" in body
    assert "B1 VISIBLE BODY" in body, "same-project embed must still inline"


# ─── (c) soft-deleted same-project target still skipped (existing INVARIANT) ──


@pytest.mark.asyncio
async def test_export_still_skips_soft_deleted_same_project_target(
    client, test_db, admin_user, regular_user,
):
    """The pre-existing soft-delete skip keeps holding under the project wall:
    a soft-deleted SAME-project target never inlines into export."""
    _, admin_token = admin_user
    user_uid, user_token = regular_user

    pid, idx = await _mk_project(client, admin_token, "Wall SoftDel")
    await _add_member(client, admin_token, pid, user_uid)

    s = await _mk_doc(
        client, pid, admin_token, title="Ghost", parent_id=idx,
        content="GHOST LEAK BODY",
    )
    await test_db.query(
        "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
        {"id": s},
    )
    d2 = await _mk_doc(
        client, pid, admin_token, title="Host", parent_id=idx,
        content=f"![s](doc:{s})",
    )

    body = await _export_md(client, user_token, d2)
    assert "GHOST LEAK BODY" not in body
    assert f"![s](doc:{s})" in body

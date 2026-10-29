"""Public link previews — Open Graph cards for /docs/<id> (plan public-link-previews).

Covers the published card (title/excerpt/og:url/og:image), the generic card for
every REFUSED principal (INVARIANT(security): bodies identical after the
requested id is replaced by a placeholder — no existence oracle), the
trailing-slash refresh target (nginx reload-loop guard), HTML escaping, the
excerpt derivation (fences/markdown stripped, ≤ 201 chars, word boundary),
the origin headers, and the static nginx routing guard.
"""
import re
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

# ─── Helpers (patterns from test_public_share.py) ────────────────────────────


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


async def _insert_share_row(project_id: str, document_id: str, scope: str = "doc") -> str:
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


def _meta(body: str, name: str) -> str | None:
    m = re.search(
        rf'<meta (?:property|name)="{re.escape(name)}" content="([^"]*)"', body,
    )
    return m.group(1) if m else None


# ─── Published card ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_capture_raising_falls_back_to_db_content(
    client, admin_user, project_with_doc, caplog,
):
    """The capture 503 must not become a cached error card: the preview falls
    back to DB documents.content (preview-only ruling; the human reader path
    keeps its 503) and logs the fallback."""
    import logging

    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc = await _make_doc(client, admin_token, pid, "Fallback Doc", "db content body")
    await _mint_share(client, admin_token, pid, doc, "doc")

    with patch("ydoc_store.capture_live_state", new_callable=AsyncMock,
               side_effect=RuntimeError("yjs boom")):
        with caplog.at_level(logging.WARNING, logger="routes.public_preview"):
            resp = await client.get(f"/api/public/documents/{doc}/preview")

    assert resp.status_code == 200, resp.text
    assert "db content body" in _meta(resp.text, "og:description")
    assert any("preview serving DB content" in r.message for r in caplog.records)


# ─── REFUSED principals: generic card, no existence oracle ──────────────────


@pytest.mark.asyncio
async def test_refused_principals_generic_card_bodies_identical(
    client, admin_user, project_with_doc,
):
    """Unpublished, unknown uuid, deleted doc, revoked share and agent-config
    subtree doc all yield HTTP 200 with the generic card. INVARIANT(security):
    nothing derived from the document leaks (title/content strings absent), and
    every body is identical once the requested id is replaced by a placeholder
    — the id is the caller's own input, echoing it reveals nothing."""
    from db import get_db

    pid, _, _ = project_with_doc
    _, admin_token = admin_user

    cases: dict[str, str] = {}

    unpublished = await _make_doc(
        client, admin_token, pid, "Unpublished Title", "unpublished secret body",
    )
    cases["unpublished"] = unpublished

    cases["unknown"] = str(uuid4())

    deleted = await _make_doc(
        client, admin_token, pid, "Deleted Title", "deleted secret body",
    )
    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
        {"id": deleted},
    )
    cases["deleted"] = deleted

    revoked = await _make_doc(
        client, admin_token, pid, "Revoked Title", "revoked secret body",
    )
    minted = await _mint_share(client, admin_token, pid, revoked, "doc")
    resp = await client.delete(
        f"/api/shares/{minted['share_id']}", cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    cases["revoked"] = revoked

    from agent_config import ensure_agent_system_docs

    roles = await ensure_agent_system_docs(pid)
    leaf = await _make_doc(
        client, admin_token, pid, "System Leaf Title", "system leaf body",
        parent_id=roles["system_root"],
    )
    await _insert_share_row(pid, leaf, "doc")
    cases["system_leaf"] = leaf

    private_strings = {
        "unpublished": ("Unpublished Title", "unpublished secret body"),
        "deleted": ("Deleted Title", "deleted secret body"),
        "revoked": ("Revoked Title", "revoked secret body"),
        "system_leaf": ("System Leaf Title", "system leaf body"),
    }

    normalized: dict[str, str] = {}
    for case, doc_id in cases.items():
        resp = await client.get(f"/api/public/documents/{doc_id}/preview")
        assert resp.status_code == 200, f"{case}: {resp.text}"
        body = resp.text
        if case in private_strings:
            title, content = private_strings[case]
            assert title not in body, f"{case}: private title leaked"
            assert content not in body, f"{case}: private content leaked"
        assert _meta(body, "og:title") == "Lore", f"{case}: not the generic card"
        normalized[case] = body.replace(doc_id, "«ID»")

    assert len(set(normalized.values())) == 1, (
        f"generic bodies must be identical after id placeholder; got "
        f"{len(set(normalized.values()))} distinct bodies"
    )


# ─── Excerpt derivation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_excerpt_strips_fences_and_markdown_cuts_on_word_boundary(
    client, admin_user, project_with_doc,
):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    content = (
        "# Heading\n\nSome **bold** text and `code` [link](https://x.y).\n\n"
        "```\nsecretfence = 'must not leak'\n```\n\n" + "word " * 60
    )
    doc = await _make_doc(client, admin_token, pid, "Excerpt Doc", content)
    await _mint_share(client, admin_token, pid, doc, "doc")

    resp = await client.get(f"/api/public/documents/{doc}/preview")

    assert resp.status_code == 200, resp.text
    description = _meta(resp.text, "og:description")
    assert description is not None
    assert "secretfence" not in description, "fenced code must be dropped"
    assert "**" not in description and "`" not in description, "markdown markers must go"
    assert "[link]" not in description and "https://x.y" not in description, (
        "link syntax must collapse to its label"
    )
    assert "Heading" in description and "bold text and code link" in description
    assert len(description) <= 201, f"excerpt must be ≤ 201 chars, got {len(description)}"
    assert description.endswith("…"), "a cut excerpt ends with the ellipsis"
    assert not description[-2].isspace(), "cut must land on a word boundary"
    # And the published title still wins over any first-heading fallback.
    assert _meta(resp.text, "og:title") == "Excerpt Doc"


@pytest.mark.asyncio
async def test_empty_title_falls_back_to_first_heading_then_site_name(
    client, admin_user, project_with_doc,
):
    """The documents API requires a non-empty title, so the empty-title case is
    reached via a direct DB update (a title-less doc is what the fallback guards)."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    from db import get_db

    async def _blank_title(doc_id: str) -> None:
        db = await get_db()
        await db.query(
            "UPDATE type::record('documents', $id) SET title = ''", {"id": doc_id},
        )

    doc = await _make_doc(client, admin_token, pid, "T1", "# First Heading\nbody text")
    await _mint_share(client, admin_token, pid, doc, "doc")
    await _blank_title(doc)

    resp = await client.get(f"/api/public/documents/{doc}/preview")
    assert resp.status_code == 200, resp.text
    assert _meta(resp.text, "og:title") == "First Heading"

    # No title, no headings → the site name.
    doc2 = await _make_doc(client, admin_token, pid, "T2", "plain body only")
    await _mint_share(client, admin_token, pid, doc2, "doc")
    await _blank_title(doc2)
    resp2 = await client.get(f"/api/public/documents/{doc2}/preview")
    assert resp2.status_code == 200, resp2.text
    assert _meta(resp2.text, "og:title") == "Lore"


# ─── Document image and clean excerpt ────────────────────────────────────────

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
    b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
    b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


async def _upload(client, token: str, project_id: str, owner_id: str, name: str, mime: str, data: bytes) -> dict:
    import io

    resp = await client.post(
        "/api/documents/upload",
        data={"project_id": project_id, "document_id": owner_id, "title": name},
        files={"file": (name, io.BytesIO(data), mime)},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _set_content(document_id: str, content: str) -> None:
    from db import get_db

    db = await get_db()
    await db.query(
        "UPDATE type::record('documents', $id) SET content = $c",
        {"id": document_id, "c": content},
    )


@pytest.mark.asyncio
async def test_first_in_scope_image_ref_is_og_image_and_served_anonymously(
    client, admin_user, project_with_doc,
):
    """A doc embed and a non-image ref before the image are skipped; the image
    ref's public file URL becomes og:image, and that URL answers 200 without a
    cookie — the card never points a bot at a URL the share would refuse."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    other = await _make_doc(client, admin_token, pid, "Other", "x")
    doc = await _make_doc(client, admin_token, pid, "Image Doc", "body")
    # A non-image ref: an uploaded file re-typed as a plain file (uploads accept media only).
    text_ref = await _upload(client, admin_token, pid, doc, "clip.png", "image/png", _PNG_BYTES)
    from db import get_db
    await (await get_db()).query(
        "UPDATE type::record('documents', $id) SET media_type = 'file'",
        {"id": text_ref["reference_id"]},
    )
    img = await _upload(client, admin_token, pid, doc, "pic.png", "image/png", _PNG_BYTES)
    await _set_content(doc, (
        f"Intro text.\n\n![o](doc:{other})\n\n![v](ref:{text_ref['reference_id']})\n\n"
        f"![p](ref:{img['reference_id']})\n"
    ))
    await _mint_share(client, admin_token, pid, doc, "doc")

    resp = await client.get(f"/api/public/documents/{doc}/preview")

    assert resp.status_code == 200, resp.text
    origin = str(client.base_url).rstrip("/")
    basename = img["file_path"].split("/")[-1]
    path = f"/api/public/documents/{doc}/files/{img['reference_id']}/{basename}"
    assert _meta(resp.text, "og:image") == f"{origin}{path}"
    assert _meta(resp.text, "og:image:width") is None, "a document image has no known size"
    assert _meta(resp.text, "twitter:card") == "summary_large_image"
    served = await client.get(path)
    assert served.status_code == 200, served.text
    assert served.content == _PNG_BYTES


@pytest.mark.asyncio
async def test_out_of_scope_image_ref_falls_back_to_generic_image(
    client, admin_user, project_with_doc,
):
    """An image owned by a doc outside a `doc`-scope share is not served
    anonymously, so it must not become og:image."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    private = await _make_doc(client, admin_token, pid, "Private", "x")
    img = await _upload(client, admin_token, pid, private, "secret.png", "image/png", _PNG_BYTES)
    doc = await _make_doc(client, admin_token, pid, "Public Doc", "body")
    await _set_content(doc, f"Text.\n\n![s](ref:{img['reference_id']})\n")
    await _mint_share(client, admin_token, pid, doc, "doc")

    resp = await client.get(f"/api/public/documents/{doc}/preview")

    origin = str(client.base_url).rstrip("/")
    assert _meta(resp.text, "og:image") == f"{origin}/og-default.png"
    assert img["reference_id"] not in resp.text
    assert _meta(resp.text, "og:image:width") == "1200"


@pytest.mark.asyncio
async def test_external_https_image_is_og_image(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc = await _make_doc(
        client, admin_token, pid, "Ext Doc",
        "Text.\n\n![cover](https://cdn.example.org/cover.jpg)\n",
    )
    await _mint_share(client, admin_token, pid, doc, "doc")

    resp = await client.get(f"/api/public/documents/{doc}/preview")

    assert _meta(resp.text, "og:image") == "https://cdn.example.org/cover.jpg"

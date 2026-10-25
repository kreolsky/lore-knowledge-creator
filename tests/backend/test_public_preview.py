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


def _refresh_target(body: str) -> str | None:
    m = re.search(r'<meta http-equiv="refresh" content="0;url=([^"]*)"', body)
    return m.group(1) if m else None


# ─── Published card ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_published_doc_card_carries_title_excerpt_url_image(
    client, admin_user, project_with_doc,
):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc = await _make_doc(
        client, admin_token, pid, "Published Doc", "published body text here",
    )
    await _mint_share(client, admin_token, pid, doc, "doc")

    resp = await client.get(f"/api/public/documents/{doc}/preview")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "public, max-age=300"
    body = resp.text
    assert _meta(body, "og:title") == "Published Doc"
    assert "published body text here" in _meta(body, "og:description")
    origin = str(client.base_url).rstrip("/")
    assert _meta(body, "og:url") == f"{origin}/docs/{doc}"
    assert _meta(body, "og:image") == f"{origin}/og-default.png"
    assert _meta(body, "og:site_name") == "Lore"
    assert _meta(body, "og:type") == "article"
    assert _meta(body, "og:image:width") == "1200"
    assert _meta(body, "og:image:height") == "630"
    assert _meta(body, "twitter:card") == "summary_large_image"
    assert "<title>Published Doc</title>" in body


@pytest.mark.asyncio
async def test_doc_under_subtree_share_carries_its_own_title(
    client, admin_user, project_with_doc,
):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    root = await _make_doc(client, admin_token, pid, "Root Title", "root body")
    child = await _make_doc(
        client, admin_token, pid, "Child Title", "child body", parent_id=root,
    )
    await _mint_share(client, admin_token, pid, root, "subtree")

    resp = await client.get(f"/api/public/documents/{child}/preview")

    assert resp.status_code == 200, resp.text
    assert _meta(resp.text, "og:title") == "Child Title", (
        "a subtree descendant must show ITS title, not the share root's"
    )


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


@pytest.mark.asyncio
async def test_refresh_target_has_trailing_slash_never_reenters_bot_location(
    client, admin_user, project_with_doc,
):
    """The refresh target is /docs/<id>/ WITH the trailing slash: the nginx bot
    location matches ^/docs/([^/]+)$, so a slash-less target would make a bot
    reload the preview forever. /docs/<id>/ falls to the SPA location."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc = await _make_doc(client, admin_token, pid, "Loop Doc", "body")
    await _mint_share(client, admin_token, pid, doc, "doc")

    resp = await client.get(f"/api/public/documents/{doc}/preview")

    target = _refresh_target(resp.text)
    assert target == f"/docs/{doc}/", f"refresh target must be /docs/<id>/: {target}"
    assert re.match(r"^/docs/[^/]+$", target) is None, (
        "refresh target must NOT match the nginx bot location (reload loop)"
    )


@pytest.mark.asyncio
async def test_title_html_escaped(client, admin_user, project_with_doc):
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc = await _make_doc(
        client, admin_token, pid, '<script>"&', "body",
    )
    await _mint_share(client, admin_token, pid, doc, "doc")

    resp = await client.get(f"/api/public/documents/{doc}/preview")

    assert resp.status_code == 200, resp.text
    assert "<script>" not in resp.text
    # The attribute carries the ESCAPED form; html-unescaping it yields the title.
    assert _meta(resp.text, "og:title") == "&lt;script&gt;&quot;&amp;"
    assert "&lt;script&gt;&quot;&amp;" in resp.text


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


# ─── Origin headers ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forwarded_proto_and_host_build_absolute_origin(
    client, admin_user, project_with_doc,
):
    """X-Forwarded-Proto first hop + Host header — same rule as the session
    cookie's scheme detection; og:url/og:image must come out https absolute."""
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    doc = await _make_doc(client, admin_token, pid, "Origin Doc", "body")
    await _mint_share(client, admin_token, pid, doc, "doc")

    resp = await client.get(
        f"/api/public/documents/{doc}/preview",
        headers={"X-Forwarded-Proto": "https", "Host": "example.org"},
    )

    assert resp.status_code == 200, resp.text
    assert _meta(resp.text, "og:url") == f"https://example.org/docs/{doc}"
    assert _meta(resp.text, "og:image") == "https://example.org/og-default.png"


# ─── Static nginx routing guard ──────────────────────────────────────────────

_NGINX_CONF_CANDIDATES = (
    "/frontend/nginx.conf",
    "frontend/nginx.conf",
)


def _read_nginx_conf() -> str:
    import pathlib

    for c in _NGINX_CONF_CANDIDATES:
        if pathlib.Path(c).exists():
            return pathlib.Path(c).read_text()
    pytest.skip("frontend/nginx.conf not reachable from the test container")


def _block(text: str, opening_re: str) -> str | None:
    """The { ... } block whose opening line matches opening_re (brace-counted)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.search(opening_re, line.strip()):
            depth = 0
            started = False
            block: list[str] = []
            for j in range(i, len(lines)):
                block.append(lines[j])
                depth += lines[j].count("{") - lines[j].count("}")
                if "{" in lines[j]:
                    started = True
                if started and depth == 0:
                    return "\n".join(block)
            return "\n".join(block)
    return None


def test_nginx_bot_map_has_default_zero_and_known_uas():
    text = _read_nginx_conf()
    block = _block(text, r"^map \$http_user_agent \$lore_link_bot \{")
    assert block is not None, (
        "nginx.conf lost the $lore_link_bot map — /docs/<id> previews are dead"
    )
    assert re.search(r"^\s*default\s+0\s*;", block, re.M), (
        "the bot map MUST default to 0 (humans keep the SPA)"
    )
    pattern = re.search(r'"~\*\(([^)]+)\)"\s+1\s*;', block)
    assert pattern, f"the bot map must carry a ~* UA pattern: {block}"
    uas = pattern.group(1)
    assert "TelegramBot" in uas and "WhatsApp" in uas, (
        f"the UA list must name TelegramBot and WhatsApp, got: {uas}"
    )


def test_nginx_docs_location_rewrites_bots_to_preview():
    text = _read_nginx_conf()
    block = _block(text, r"^location ~ \^/docs/\(\[\^/\]\+\)\$")
    assert block is not None, (
        "nginx.conf lost the /docs/<id> location — every preview request "
        "falls through to the SPA"
    )
    assert (
        "rewrite ^/docs/([^/]+)$ /api/public/documents/$1/preview last;" in block
    ), f"the bot rewrite must re-enter location /api/ via last: {block}"
    assert "try_files /index.html =404;" in block, (
        "non-bot clients must keep the SPA fallback"
    )


def test_generic_description_matches_spa_shell_title():
    """The generic card's description restates the SPA shell's <title>: for a
    /docs/<id> URL a bot sees one or the other, so the tagline must not drift
    (frontend/index.html is the source of truth)."""
    import pathlib

    from routes.public_preview import _GENERIC_DESCRIPTION

    for c in ("/frontend/index.html", "frontend/index.html"):
        if pathlib.Path(c).exists():
            m = re.search(r"<title>([^<]*)</title>", pathlib.Path(c).read_text())
            assert m, "frontend/index.html lost its <title>"
            assert _GENERIC_DESCRIPTION == m.group(1), (
                "the preview generic description drifted from the SPA shell title"
            )
            return
    pytest.skip("frontend/index.html not reachable from the test container")

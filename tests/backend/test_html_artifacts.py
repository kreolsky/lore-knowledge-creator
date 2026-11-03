"""HTML artifacts v1 — the server half.

Covers the artifact seam (see SYSTEM: artifact-serve): the cookieless origin (host-split middleware, signed
short-lived URLs, /a/<id> + /lib/*), the agent create/edit surface for
node_type="html", the leaf rule in code AND schema, the embeddings / lexical
search / mentions skips, and the export degradation. Three DENSE tests by the
suite's test-diet rule (test-why gate: at most 3 new per commit).
"""
import hashlib
import hmac as hmac_mod
import io
import secrets
import time
import zipfile

import pytest
from helpers import pin_artifact_cap, pin_artifact_origin

TEST_ORIGIN = "https://lore-usercontent.test"
ARTIFACT_HOST = {"host": "lore-usercontent.test"}

PAGE = (
    "<!doctype html><html><head><title>Dashboard</title></head>"
    "<body><h1>Revenue dashboard</h1><div>zz-marker-div</div></body></html>"
)


# ─── Helpers ─────────────────────────────────────────────────────────────────


async def _make_agent_key(test_db, user_id, project_id):
    from db import create_record

    token = "lore_" + secrets.token_hex(32)
    await create_record("api_keys", f"ha-key-{secrets.token_hex(4)}", {
        "user_id": user_id, "project_id": project_id,
        "document_id": "",
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "agent", "capabilities": ["agent"],
        "auto_apply": True, "internal": False,
    })
    return token


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


async def _make_doc(client, token, project_id, title, content="", parent_id=None):
    body = {"project_id": project_id, "title": title, "content": content}
    if parent_id is not None:
        body["parent_id"] = parent_id
    resp = await client.post("/api/documents", json=body,
                             cookies={"lore_session": token})
    assert resp.status_code == 200, resp.text
    return resp.json()["document_id"]


async def _create_html(client, agent_token, project_id, parent_id, title, content):
    return await client.post("/api/tool/create_document", json={
        "title": title, "content": content, "parent_id": parent_id,
        "node_type": "html", "apply": "auto",
    }, headers=_hdr(agent_token))


async def _row(test_db, doc_id):
    rows = await test_db.query(
        "SELECT media_type, is_reference, parent_id, content, title "
        "FROM type::record('documents', $id)",
        {"id": doc_id},
    )
    assert rows, f"row {doc_id} missing"
    from db import extract_id

    row = dict(rows[0])
    row["parent_id"] = extract_id(row["parent_id"]) if row.get("parent_id") else None
    return row


async def _seed(client, test_db, admin_user, project_with_doc):
    """(agent_key, parent, html_id, other_html_id, plain_id) for the project."""
    pid, _, admin_uid = project_with_doc
    _, token = admin_user
    parent = await _make_doc(client, token, pid, "Host doc")
    agent_key = await _make_agent_key(test_db, admin_uid, pid)
    html_id = (await _create_html(
        client, agent_key, pid, parent, "Dashboard", PAGE,
    )).json()["doc_id"]
    other_id = (await _create_html(
        client, agent_key, pid, parent, "Other page", "<p>other</p>",
    )).json()["doc_id"]
    plain_id = await _make_doc(client, token, pid, "Plain doc", "plain markdown")
    return agent_key, parent, html_id, other_id, plain_id


def _assert_sandbox_headers(served) -> None:
    assert served.headers["content-security-policy"].startswith("sandbox allow-scripts")
    assert "set-cookie" not in served.headers
    assert served.headers["cache-control"] == "private, no-store"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.headers["referrer-policy"] == "no-referrer"
    directives = {
        d.split(" ")[0]: d.split(" ")[1:]
        for d in served.headers["content-security-policy"].split("; ") if d.strip()
    }
    assert directives["default-src"] == ["'none'"]
    assert directives["connect-src"] == ["'none'"]
    assert directives["form-action"] == ["'none'"]
    # No third-party host in any RESOURCE directive — every scheme-bearing
    # value is THIS artifact origin (the /lib/ prefix) or data:/blob:. The
    # framers in frame-ancestors (the app origins) are the one exception.
    for directive, values in directives.items():
        if directive == "frame-ancestors":
            continue
        for v in values:
            if "://" in v or v.startswith(("data:", "blob:")):
                assert v.startswith((f"{TEST_ORIGIN}/lib/", "data:", "blob:")), (
                    f"external host in {directive}: {v}"
                )
    assert directives["frame-ancestors"], "frame-ancestors must name the app origins"


# ═══ /a/<id>, the host split, /lib/* ═════════════════════════════════════════


# TEST-WHY: refused-principal — every serve-side wall: forged/expired/other-doc/markdown tokens 403, /api on the artifact host and /a//lib elsewhere 404, manifest-external /lib 404.
async def test_artifact_serve_security(client, test_db, admin_user, project_with_doc, monkeypatch):
    """Every refusal the cookieless serve path owes, plus the headers."""
    import artifacts

    pin_artifact_origin(monkeypatch)
    _, _, html_id, other_id, plain_id = await _seed(
        client, test_db, admin_user, project_with_doc,
    )

    async def _get(doc_id: str, t: str):
        return await client.get(f"/a/{doc_id}?t={t}", headers=ARTIFACT_HOST)

    # The honest token serves the page with the sandbox header set.
    good = artifacts.mint_artifact_url(html_id).split("t=")[1]
    assert artifacts.mint_artifact_url(html_id).startswith(
        f"{TEST_ORIGIN}/a/{html_id}?t="
    )
    served = await _get(html_id, good)
    assert served.status_code == 200, served.text
    assert served.text == PAGE
    assert served.headers["content-type"].startswith("text/html")
    _assert_sandbox_headers(served)

    assert (await _get(html_id, f"{int(time.time()) + 600}.{'0' * 64}")).status_code == 403
    exp = int(time.time()) - 1
    sig = hmac_mod.new(
        artifacts._signing_key(), f"{html_id}:{exp}".encode(), hashlib.sha256,
    ).hexdigest()
    assert (await _get(html_id, f"{exp}.{sig}")).status_code == 403
    other_token = artifacts.mint_artifact_url(other_id).split("t=")[1]
    assert (await _get(html_id, other_token)).status_code == 403
    plain_token = artifacts.mint_artifact_url(plain_id).split("t=")[1]
    refused = await _get(plain_id, plain_token)
    assert refused.status_code == 403
    assert "plain markdown" not in refused.text
    _assert_sandbox_headers(refused)

    # Host split: the artifact host serves ONLY /a/* and /lib/*.
    _, admin_token = admin_user
    resp = await client.get(
        "/api/projects", headers=ARTIFACT_HOST, cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 404, resp.text
    # The app host never serves the artifact namespace (token never reaches a
    # handler), and hosting-unset serves it nowhere.
    assert (await client.get(f"/a/{html_id}?t={good}")).status_code == 404
    assert (await client.get("/lib/echarts@5.6.0/echarts.min.js")).status_code == 404
    pin_artifact_origin(monkeypatch, "")
    assert (await client.get(f"/a/{html_id}?t={good}", headers=ARTIFACT_HOST)).status_code == 404
    assert (await client.get("/a/whatever?t=1.2", headers=ARTIFACT_HOST)).status_code == 404

    # /lib/* resolves ONLY manifest names — an unknown library, an unpinned
    # file and a traversal-shaped path are all 404 before any file access.
    pin_artifact_origin(monkeypatch)
    assert (await client.get("/lib/nope@9/nope.js", headers=ARTIFACT_HOST)).status_code == 404
    assert (await client.get("/lib/echarts@5.6.0/evil.js", headers=ARTIFACT_HOST)).status_code == 404
    assert (await client.get(
        "/lib/echarts@5.6.0/%2e%2e/fetch.py", headers=ARTIFACT_HOST,
    )).status_code == 404


# ═══ artifact-url mint routes + the boot guard ══════════════════════════════


# TEST-WHY: refused-principal — the mint walls: stranger/anonymous/public-without-share refused, revoke closes, unset hosting a named 422, boot guard refuses sibling subdomains.
async def test_artifact_url_mint_access(client, test_db, admin_user, regular_user, project_with_doc, monkeypatch):
    """Who may mint an artifact URL, and which boots are refused."""
    import artifacts
    from artifacts import check_artifact_origin_config
    from helpers import make_token
    from settings_registry import ConfigError

    from db import create_record

    pin_artifact_origin(monkeypatch)
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    _, _, html_id, _, _ = await _seed(client, test_db, admin_user, project_with_doc)

    # A readonly member mints; a stranger and an anonymous caller are refused.
    viewer_uid, viewer_token = regular_user
    await create_record("project_members", "ha-member-001", {
        "project_id": pid, "user_id": viewer_uid, "access_level": "readonly",
    })
    resp = await client.get(
        f"/api/documents/{html_id}/artifact-url", cookies={"lore_session": viewer_token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["url"].startswith(f"{TEST_ORIGIN}/a/{html_id}?t=")
    assert body["expires_in"] > 0

    await create_record("users", "ha-stranger-001", {
        "name": "stranger", "email": "stranger@test.com",
        "password_hash": "x", "role": "user", "user_facts": "",
    })
    stranger = make_token("ha-stranger-001", "stranger", "user", "s@test.com")
    resp = await client.get(
        f"/api/documents/{html_id}/artifact-url", cookies={"lore_session": stranger},
    )
    assert resp.status_code in (403, 404), resp.text
    assert (await client.get(
        f"/api/documents/{html_id}/artifact-url",
    )).status_code in (401, 403)

    # The public funnel: no share ⇒ uniform 404; a live doc-scope share mints
    # a URL that serves anonymously; revoking closes the mint.
    assert (await client.get(
        f"/api/public/documents/{html_id}/artifact-url",
    )).status_code == 404
    resp = await client.post(
        f"/api/projects/{pid}/documents/{html_id}/shares",
        json={"scope": "doc"}, cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    resp = await client.get(f"/api/public/documents/{html_id}/artifact-url")
    assert resp.status_code == 200, resp.text
    served = await client.get(
        resp.json()["url"].removeprefix(TEST_ORIGIN), headers=ARTIFACT_HOST,
    )
    assert served.status_code == 200 and served.text == PAGE
    from db import get_db

    await (await get_db()).query(
        "UPDATE document_shares SET deleted_at = time::now() WHERE document_id = $did",
        {"did": html_id},
    )
    assert (await client.get(
        f"/api/public/documents/{html_id}/artifact-url",
    )).status_code == 404

    # Hosting-unset is a NAMED refusal (an explicit error state — 501, the
    # client's not-configured signal: a status needs no body parsing and
    # covers the guest, who has no /me), and a non-artifact row is refused
    # with 422.
    pin_artifact_origin(monkeypatch, "")
    resp = await client.get(
        f"/api/documents/{html_id}/artifact-url", cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 501 and "not configured" in resp.text
    pin_artifact_origin(monkeypatch)
    plain = await _make_doc(client, admin_token, pid, "Plain2", "x")
    resp = await client.get(
        f"/api/documents/{plain}/artifact-url", cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 422 and "not an html artifact" in resp.text

    # Boot guard (pure algebra): unset CORS list, same host and sibling
    # subdomain refuse; a genuinely separate registrable domain passes.
    check_artifact_origin_config([], False, origin_value="")
    with pytest.raises(ConfigError, match="CORS_ORIGINS"):
        check_artifact_origin_config(
            ["http://localhost:5173"], False, origin_value=TEST_ORIGIN,
        )
    with pytest.raises(ConfigError, match="registrable domain"):
        check_artifact_origin_config(
            ["https://lore.example.com"], True, origin_value="https://lore.example.com",
        )
    with pytest.raises(ConfigError, match="registrable domain"):
        check_artifact_origin_config(
            ["https://app.example.com"], True, origin_value="https://art.example.com",
        )
    check_artifact_origin_config(
        ["https://lore.example.com"], True, origin_value="https://usercontent.example.org",
    )
    assert artifacts._hosts_conflict("art.example.com", "app.example.com")
    assert not artifacts._hosts_conflict("192.168.1.10", "168.1.10")


# ═══ agent surface + row rules + skips + export ═════════════════════════════


# TEST-WHY: cross-process-contract — the html row over the real tool+HTTP+Surreal path: verbatim create/edit, caps, leaf rule in route AND schema, fixed kind, title-only search, embed/mention skips, .html export.
async def test_html_agent_surface_and_rules(client, test_db, admin_user, project_with_doc, monkeypatch):
    """The row's whole life, contract by contract."""
    pin_artifact_origin(monkeypatch)
    pid, _, _ = project_with_doc
    _, admin_token = admin_user
    parent = await _make_doc(client, admin_token, pid, "Host doc")
    agent_key = await _make_agent_key(test_db, admin_user[0], pid)

    # Hosting-unset: the tool refuses with the named 422 and the description
    # sentence renders empty; the served enum keeps the kind unconditionally.
    from agent_tools.specs.mutate import MUTATE_TOOLS
    from artifacts import html_node_sentence

    pin_artifact_origin(monkeypatch, "")
    resp = await _create_html(client, agent_key, pid, None, "D", PAGE)
    assert resp.status_code == 422 and "not configured" in resp.text
    assert html_node_sentence() == ""
    pin_artifact_origin(monkeypatch)
    assert html_node_sentence() and "/lib/" in html_node_sentence()

    # Create: a real html leaf, stored verbatim (no normalize/unwrap ran).
    page = (
        "<html><body>\n\n\n<h1>Title</h1>\n\n\n"
        "<p>see (<a href='#'>doc</a>)</p>\n\n\n"
        "<ul><li>   - item</li></ul>\n\n\n</body></html>"
    )
    resp = await _create_html(client, agent_key, pid, parent, "Page", page)
    assert resp.status_code == 200, resp.text
    doc_id = resp.json()["doc_id"]
    row = await _row(test_db, doc_id)
    assert row["media_type"] == "html"
    assert row["is_reference"] is False
    assert row["parent_id"] == parent
    assert row["content"] == page
    enum = next(t for t in MUTATE_TOOLS if t["function"]["name"] == "create_document"
                )["function"]["parameters"]["properties"]["node_type"]["enum"]
    assert "html" in enum

    # Edit: the stored diff is EXACTLY the asked change (the plan's mangle
    # risk); markdown-shaped tools refuse the row.
    resp = await client.post("/api/tool/edit_document", json={
        "document_id": doc_id,
        "edits": [{"old_string": "<h1>Title</h1>", "new_string": "<h1>Renamed</h1>"}],
        "apply": "auto",
    }, headers=_hdr(agent_key))
    assert resp.status_code == 200, resp.text
    assert (await _row(test_db, doc_id))["content"] == page.replace(
        "<h1>Title</h1>", "<h1>Renamed</h1>",
    )
    resp = await client.post("/api/tool/append_to_document", json={
        "document_id": doc_id, "content": "more", "apply": "auto",
    }, headers=_hdr(agent_key))
    assert resp.status_code == 422 and "edit_document" in resp.text
    assert (await client.post("/api/tool/create_table", json={
        "document_id": doc_id, "rows": [["a", "b"]], "apply": "auto",
    }, headers=_hdr(agent_key))).status_code == 422

    # Size cap: create past the cap is a named 422; an edit crossing it too.
    # The cap is pinned SMALL through the artifacts module (helpers.
    # pin_artifact_cap) — every writer reads it at call time, so ONE pin
    # covers create and edit growth without posting multi-MiB bodies over
    # the HTTP test path.
    cap = pin_artifact_cap(monkeypatch, 300 * 1024)
    assert (await _create_html(
        client, agent_key, pid, parent, "Huge",
        "<p>" + "x" * (cap + 1) + "</p>",
    )).status_code == 422
    near_id = (await _create_html(
        client, agent_key, pid, parent, "Near",
        "<p>" + "a" * (cap - 100) + "</p>",
    )).json()["doc_id"]
    resp = await client.post("/api/tool/edit_document", json={
        "document_id": near_id,
        "edits": [{"old_string": "<p>", "new_string": "<p>" + "b" * 200}],
        "apply": "auto",
    }, headers=_hdr(agent_key))
    assert resp.status_code == 422 and "KiB" in resp.text

    # Leaf rule: REST create under the page is a 400; a DIRECT row create is
    # aborted by the schema event (no row persists).
    resp = await client.post("/api/documents", json={
        "project_id": pid, "title": "Under page", "content": "x", "parent_id": doc_id,
    }, cookies={"lore_session": admin_token})
    assert resp.status_code == 400 and "cannot be parents" in resp.text
    from db import create_record

    with pytest.raises(Exception, match="cannot be parents"):
        await create_record("documents", "ha-child-under-html", {
            "project_id": pid, "parent_id": doc_id, "title": "Child",
            "content": "x", "is_index": False, "path": "/ha-child-under-html",
        })
    assert not (await test_db.query(
        "SELECT * FROM type::record('documents', 'ha-child-under-html')",
    )), "the THROW must abort the CREATE — no row may persist"

    # Moves: a plain move is legal; a kind conversion is a named 422. (Runs
    # LAST — the move reparents the page out of `parent`, whose subtree the
    # export section below still needs.)
    other = await _make_doc(client, admin_token, pid, "Other host")

    # Lexical search: the page is found by TITLE only — markup never matches
    # (FTS and exact), the hit carries no snippet, and a markdown sibling
    # still matches its body.
    await _make_doc(client, admin_token, pid, "Plain doc", "zz-marker-div in prose")
    for exact in ("false", "true"):
        resp = await client.get(
            f"/api/projects/{pid}/search",
            params={"q": "zz-marker-div", "exact": exact},
            cookies={"lore_session": admin_token},
        )
        ids = [r.get("document_id") or r.get("reference_id")
               for r in resp.json()["results"]]
        assert doc_id not in ids, f"markup must not match (exact={exact}): {ids}"
    resp = await client.get(
        f"/api/projects/{pid}/search", params={"q": "Page"},
        cookies={"lore_session": admin_token},
    )
    hits = [r for r in resp.json()["results"]
            if r.get("document_id") == doc_id or r.get("reference_id") == doc_id]
    assert hits and hits[0]["snippet"] is None

    # Embeddings + mentions skip html rows entirely.
    from db import get_db
    from embeddings import _fetch_reembed_parent
    from mentions import rebuild_doc_mentions

    db = await get_db()
    assert await _fetch_reembed_parent(db, doc_id) is None
    linked = await _make_doc(client, admin_token, pid, "Linked doc", "body")
    page_link = f"<p>[see]({linked})</p>"
    await client.post("/api/tool/edit_document", json={
        "document_id": doc_id,
        "edits": [{"old_string": "<h1>Renamed</h1>",
                   "new_string": f"<h1>Renamed</h1>{page_link}"}],
        "apply": "auto",
    }, headers=_hdr(agent_key))
    assert await rebuild_doc_mentions(db, "documents", doc_id, page_link) == []
    assert not (await db.query(
        "SELECT * FROM doc_mentions WHERE in = type::record('documents', $id)",
        {"id": doc_id},
    ))

    # Subtree export: `Title.html` verbatim + the embed degraded to a link.
    await _make_doc(
        client, admin_token, pid, "Referrer",
        f"see ![page](doc:{doc_id}) and [page link](doc:{doc_id})",
        parent_id=parent,
    )
    resp = await client.get(
        f"/api/documents/{parent}/export/subtree", cookies={"lore_session": admin_token},
    )
    assert resp.status_code == 200, resp.text
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = zf.namelist()
    assert "Host doc/Page.html" in names, names
    assert zf.read("Host doc/Page.html").decode() == (await _row(test_db, doc_id))["content"]
    referrer = zf.read("Host doc/Referrer.md").decode()
    assert "![page](./Page.html)" not in referrer, "an embed degrades to a link"
    assert "[page](./Page.html)" in referrer

    # Moves (last — see the note above): a plain move is legal; a kind
    # conversion is a named 422.
    assert (await client.post("/api/tool/move_document", json={
        "document_id": doc_id, "parent_id": other, "apply": "auto",
    }, headers=_hdr(agent_key))).status_code == 200
    resp = await client.post("/api/tool/move_document", json={
        "document_id": doc_id, "parent_id": other,
        "node_type": "reference", "apply": "auto",
    }, headers=_hdr(agent_key))
    assert resp.status_code == 422 and "html artifact" in resp.text

"""Personal MCP servers — the backend half.

# SYSTEM: mcp-servers (see backend/mcp_servers.py) — under test: the admin
save gate (routes/admin_mcp_servers.py), the moderator scope, the cabinet
routes, the vault isolation (list_secrets/http_request must not see server
rows), the connection-check classifier and the turn-payload renderer.

The highest-value assertions are the REFUSALS and the RENDER exclusions: a
bad auth_mode/scheme, an egress-refused host, a foreign moderator's server,
a declined, missing or host-stale key — each must leave the server OUT of
the payload or the request refused, and no plaintext value may ever ride a
response (the shared key not even a SELECT). The egress arms pin
OUTBOUND_ALLOW_CIDRS both ways (the guard reads it per call), so no test
resolves real DNS; the "keyless user pays no DNS" arm records resolve calls
instead of hitting the network.

Three consolidated tests (the test-count gate's 3-per-commit diet): the pure
definition/key gate, the admin/moderator/visibility surface, and the key
flow → render → cascade pipeline. Every refusal matrix lives as a loop inside
its owner, so a failure names the loop case in the assertion message.
"""

import secrets as _secrets

import pytest
from helpers import make_token, pin_tool_gates

TOKEN_VALUE = "sk-mcp-live-4c2-not-a-real-key"


# ─── Fixtures: users, a moderator, a group ────────────────────────────────────


async def _mk_user(test_db, uid: str, role: str = "user", moderator_id: str | None = None) -> str:
    from db import create_record

    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    await create_record("users", uid, {
        "name": uid, "email": f"{uid}@t.st", "password_hash": "x",
        "role": role, "user_facts": "", "moderator_id": moderator_id,
    })
    return uid


async def _mk_group_member(test_db, uid: str, gid: str) -> None:
    from db import create_record

    await create_record("groups", gid, {"name": f"group-{gid}"})
    await create_record("group_members", uid, {"group_id": gid, "user_id": uid})


def _cookie(token: str) -> dict:
    return {"lore_session": token}


def _def_body(
    name: str = "confluence", url: str = "https://127.0.0.1:9301/mcp",
    auth_mode: str = "personal", auth_scheme: str = "Bearer",
    help_url: str | None = None, subjects: list[str] | None = None,
    **extra: object,
) -> dict:
    return {
        "name": name,
        "title": "Confluence, engineering",
        "url": url,
        "auth_mode": auth_mode,
        "auth_scheme": auth_scheme,
        "help_url": help_url,
        "subjects": ["public"] if subjects is None else subjects,
    } | extra


async def _wipe_servers(test_db) -> None:
    """Start from a clean slate — a FAILED earlier run leaves rows behind
    (the cascades that clean up live at the END of each test)."""
    await test_db.query("DELETE mcp_servers")
    await test_db.query("DELETE user_secrets WHERE server_id != NONE")


async def _make_internal_key(user_id: str, project_id: str) -> str:
    import hashlib

    from db import create_record

    token = "lore_" + _secrets.token_hex(32)
    await create_record("api_keys", f"sk-{_secrets.token_hex(4)}", {
        "user_id": user_id, "project_id": project_id, "document_id": "",
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "label": "agent", "capabilities": ["agent"], "internal": True,
    })
    return token


# ─── 1. The pure definition/key gate ──────────────────────────────────────────


# TEST-WHY: pure-logic-edge — validate_definition's refusal matrix (name/url/auth_mode/scheme/help_url shape) plus normalize_key's own (pasted scheme, under-8, non-printable) and prompt_section's contract, zero seams, no db.
def test_validate_definition_and_key_gates():
    import mcp_servers

    refusals = {
        # http:// would carry the rendered key in cleartext.
        "http-url": {"url": "http://127.0.0.1:9301/mcp"},
        # userinfo is a second credential channel the renderer does not own.
        "userinfo-url": {"url": "https://k@127.0.0.1:9301/mcp"},
        "bad-port-url": {"url": "https://127.0.0.1:99999/mcp"},
        # dsh's serverName contract (the mcp__ prefix).
        "name-with-space": {"name": "has space"},
        "name-too-long": {"name": "x" * 33},
        # auth_mode is a closed vocabulary.
        "bad-mode": {"auth_mode": "oauth"},
        # The scheme rides the Authorization header verbatim — token grammar
        # only, or it cannot travel as a header value at all.
        "bad-scheme-space": {"auth_scheme": "Bearer X"},
        "bad-scheme-too-long": {"auth_scheme": "s" * 33},
        "help-url-not-http": {"help_url": "ftp://keys.example.com"},
    }
    accepted = []
    for label, patch in refusals.items():
        body = _def_body() | patch
        try:
            mcp_servers.validate_definition(
                name=body["name"], title=body["title"], url=body["url"],
                auth_mode=body["auth_mode"], auth_scheme=body["auth_scheme"],
                help_url=body["help_url"],
            )
        except mcp_servers.McpServerError:
            continue
        accepted.append(label)
    assert accepted == [], f"gate(s) accepted what they must refuse: {accepted}"

    # The pass-through shape: an empty title folds to the name, an empty
    # scheme is legal (the key is sent verbatim), port preserved.
    fields = mcp_servers.validate_definition(
        name="svc", title="  ", url="https://api.example.com:8443/mcp",
        auth_mode="shared", auth_scheme="", help_url="  ",
    )
    assert fields["title"] == "svc"
    assert fields["host"] == "api.example.com" and fields["port"] == 8443
    assert fields["help_url"] is None

    # normalize_key: one gate for both key paths.
    cases = [
        ("  bare-token-9f13  ", "Bearer", "bare-token-9f13"),
        ("Bearer pasted-token-9f13", "Bearer", "pasted-token-9f13"),
        ("bearer case-token-9f13", "Bearer", "case-token-9f13"),
        # An empty scheme sends the key verbatim — a pasted prefix is part
        # of the value, not a mistake to correct.
        ("Basic dXNlcjprZXk=", "", "Basic dXNlcjprZXk="),
    ]
    for value, scheme, want in cases:
        assert mcp_servers.normalize_key(value, scheme) == want, (value, scheme)
    key_refusals = {
        "under-8": ("short-7", "Bearer"),
        "under-8-after-strip": ("Bearer short", "Bearer"),
        "non-printable": ("bad\x01-key-9f13", "Bearer"),
    }
    for label, (value, scheme) in key_refusals.items():
        try:
            mcp_servers.normalize_key(value, scheme)
        except mcp_servers.McpServerError:
            continue
        pytest.fail(f"normalize_key accepted a refused key: {label}")

    # prompt_section: one line per server, sorted, the mcp__ prefix named;
    # empty entries = "" (everyone else's prompt stays byte-identical).
    assert mcp_servers.prompt_section([]) == ""
    section = mcp_servers.prompt_section([
        {"name": "beta", "title": "Lore Beta, sales"},
        {"name": "alpha", "title": "Lore Alpha,\nengineering"},
    ])
    assert section == (
        "\n\nThis project's own tools carry no prefix and are the ones to use "
        "for any request about this project — its documents, references, memory "
        "and chats. The MCP servers below are EXTERNAL services the user "
        "connected; their mcp__<server>__ tools act on that service, not on this "
        "project. Call one when the request names that service or needs "
        "something only it holds:\n"
        "alpha — Lore Alpha, engineering (tools: mcp__alpha__*)\n"
        "beta — Lore Beta, sales (tools: mcp__beta__*)"
    )

    # The gateway advertises readOnlyHint from the registry's own `mutating`
    # declaration — an unannotated read tool would park on an approval card
    # on every client that trusts the hint.
    from agent_tools.registry import mcp_entries
    from mcp_gateway.schemas import build_tool_list

    # INVARIANT: the env-gated preview_extractor is pinned visible — every
    # mcp entry must be advertised. Why: unpinned, the test reads the runner's
    # MCP_RUN_EXTRACTOR and fails on CI while passing on gray.
    advertised = {t.name: t for t in build_tool_list(preview_visible=True)}
    for entry in mcp_entries():
        tool = advertised.get(entry.name)
        assert tool is not None, entry.name
        assert tool.annotations is not None, entry.name
        assert tool.annotations.read_only_hint == (not entry.mutating), entry.name


# ─── 2. The admin save gate, the moderator scope, cabinet visibility ─────────


# TEST-WHY: pure-logic-edge — the check verdict's server detail: a server echoing the key in any form (raw, base64, url-encoded, whole header) relays [secret] instead, a non-JSON-RPC body relays only its HTTP status, a long or control-laden message is bounded, zero seams, no db.
def test_check_detail_scrubs_the_key():
    import base64
    from urllib.parse import quote

    import httpx
    import mcp_servers

    key = "mcp_graph-enricher_AV1/x+y=z"
    headers = {"Authorization": f"Bearer {key}"}

    def detail(body) -> str:
        resp = httpx.Response(401, json=body) if not isinstance(body, str) else httpx.Response(401, text=body)
        return mcp_servers._server_detail(resp, headers)

    echoes = [key, base64.b64encode(key.encode()).decode(), quote(key, safe=""), f"Bearer {key}"]
    for echo in echoes:
        got = detail({"jsonrpc": "2.0", "error": {"code": -32001, "message": f"bad key {echo} here"}})
        assert got is not None and "[secret]" in got and "AV1" not in got, (echo, got)
    assert detail({"jsonrpc": "2.0", "error": {"code": -32001, "message": "Invalid or disabled API key"}}) == "Invalid or disabled API key"
    assert detail("<html><body>403 Forbidden</body></html>") == "HTTP 401"
    assert detail({"detail": "not json-rpc"}) == "HTTP 401"
    assert detail(["not", "an", "object"]) == "HTTP 401"
    long = detail({"error": {"message": "a\tb\n" + "x" * 500}})
    assert long is not None and long.startswith("a b x") and len(long) == 200


# TEST-WHY: cross-process-contract — the save gate and the subject ladder over real infra (REST → SurrealDB rows → user_subjects, nothing patched): an egress-refused host never lands a row, a moderator's surface is their own single-subject servers only (403 at create, 404 on foreign rows), the cabinet list follows the group grants, and the shared key never rides a response nor a plain SELECT as plaintext.
@pytest.mark.asyncio
async def test_admin_gate_moderator_scope_and_visibility(
    client, test_db, admin_user, regular_user, monkeypatch,
):
    from db import fetch_one

    await _wipe_servers(test_db)

    # An egress-refused host (private address, allowlist empty) → 422.
    pin_tool_gates(monkeypatch, outbound_cidrs="")
    resp = await client.post(
        "/api/admin/mcp-servers", json=_def_body(), cookies=_cookie(admin_user[1]),
    )
    assert resp.status_code == 422, resp.text
    assert "refused" in resp.text or "resolve" in resp.text

    # With the range allowed the same definition lands; duplicates 409;
    # unknown and empty subject lists 422; the row carries created_by.
    pin_tool_gates(monkeypatch, outbound_cidrs="127.0.0.0/8")
    resp = await client.post(
        "/api/admin/mcp-servers", json=_def_body(), cookies=_cookie(admin_user[1]),
    )
    assert resp.status_code in (200, 201), resp.text
    server_id = resp.json()["id"]
    assert resp.json()["host"] == "127.0.0.1:9301"
    assert resp.json()["auth_mode"] == "personal"
    assert resp.json()["shared_key_set"] is False
    for label, body in (
        ("duplicate-name", _def_body()),
        ("unknown-subject", _def_body(subjects=["group:nope"])),
        ("no-subjects", _def_body(subjects=[])),
        ("bad-mode", _def_body(auth_mode="oauth")),
    ):
        resp = await client.post(
            "/api/admin/mcp-servers", json=body, cookies=_cookie(admin_user[1]),
        )
        assert resp.status_code == (409 if label == "duplicate-name" else 422), (label, resp.text)
    assert (await fetch_one("mcp_servers", server_id))["created_by"] == admin_user[0]

    # A shared key: saved write-only, answered as shared_key_set, ciphertext
    # the ONLY trace in a raw SELECT — and a url move to another host
    # answers shared_needs_reenter.
    resp = await client.post(
        "/api/admin/mcp-servers",
        json=_def_body(name="teamkey", auth_mode="shared", shared_key=TOKEN_VALUE),
        cookies=_cookie(admin_user[1]),
    )
    assert resp.status_code in (200, 201), resp.text
    shared_id = resp.json()["id"]
    view = resp.json()
    assert view["shared_key_set"] is True and view["shared_needs_reenter"] is False
    assert TOKEN_VALUE not in resp.text
    row = await fetch_one("mcp_servers", shared_id)
    assert TOKEN_VALUE not in str(row)
    assert row["shared_ciphertext"] and row["shared_host"] == "127.0.0.1:9301"
    resp = await client.get("/api/admin/mcp-servers", cookies=_cookie(admin_user[1]))
    assert all(TOKEN_VALUE not in str(s) for s in resp.json()), resp.text
    # A shared key on a personal server is a 422, not a silent ignore.
    resp = await client.patch(
        f"/api/admin/mcp-servers/{server_id}", json={"shared_key": TOKEN_VALUE},
        cookies=_cookie(admin_user[1]),
    )
    assert resp.status_code == 422, resp.text
    # The url moves to another host → the stored key is stranded.
    resp = await client.patch(
        f"/api/admin/mcp-servers/{shared_id}",
        json={"url": "https://127.0.0.1:9302/mcp"},
        cookies=_cookie(admin_user[1]),
    )
    assert resp.status_code == 200 and resp.json()["shared_needs_reenter"] is True, resp.text

    # The moderator scope: public/group/mixed create → 403; own mod: lands;
    # a foreign server does not exist (list/PATCH/DELETE); subjects are not
    # the moderator's to change; their own fields PATCH fine.
    mod_uid = await _mk_user(test_db, "mcp-mod-1", role="moderator")
    mod_token = make_token(mod_uid, "mcp-mod-1", "moderator")
    from db import create_record

    await create_record("groups", "mcp-admin-group", {"name": "g"})
    for subjects in (
        ["public"], ["group:mcp-admin-group"], ["public", f"mod:{mod_uid}"],
    ):
        resp = await client.post(
            "/api/admin/mcp-servers", json=_def_body(subjects=subjects),
            cookies=_cookie(mod_token),
        )
        assert resp.status_code == 403, (subjects, resp.text)
    resp = await client.post(
        "/api/admin/mcp-servers",
        json=_def_body(name="modserv", subjects=[f"mod:{mod_uid}"]),
        cookies=_cookie(mod_token),
    )
    assert resp.status_code in (200, 201), resp.text
    own_id = resp.json()["id"]
    resp = await client.get("/api/admin/mcp-servers", cookies=_cookie(mod_token))
    assert [s["id"] for s in resp.json()] == [own_id]
    resp = await client.patch(
        f"/api/admin/mcp-servers/{server_id}", json={"title": "x"},
        cookies=_cookie(mod_token),
    )
    assert resp.status_code == 404, resp.text
    resp = await client.delete(
        f"/api/admin/mcp-servers/{server_id}", cookies=_cookie(mod_token),
    )
    assert resp.status_code == 404, resp.text
    resp = await client.patch(
        f"/api/admin/mcp-servers/{own_id}", json={"title": "My team's"},
        cookies=_cookie(mod_token),
    )
    assert resp.status_code == 200 and resp.json()["title"] == "My team's"
    resp = await client.patch(
        f"/api/admin/mcp-servers/{own_id}", json={"subjects": ["public"]},
        cookies=_cookie(mod_token),
    )
    assert resp.status_code == 403, resp.text

    # Cabinet visibility: a group member sees the group's + public servers, a
    # plain user only public, a disabled server nobody; an invisible server
    # does not exist for the cabinet writes either. A regular user never
    # reaches the admin surface.
    member_uid = await _mk_user(test_db, "mcp-member")
    await _mk_group_member(test_db, member_uid, "mcp-group-1")
    member_token = make_token(member_uid, "mcp-member")
    resp = await client.post(
        "/api/admin/mcp-servers",
        json=_def_body(name="groupserv", subjects=["group:mcp-group-1"]),
        cookies=_cookie(admin_user[1]),
    )
    gid = resp.json()["id"]
    resp = await client.get("/api/cabinet/mcp", cookies=_cookie(member_token))
    assert {s["name"] for s in resp.json()["servers"]} == {"groupserv", "confluence", "teamkey"}
    resp = await client.get("/api/cabinet/mcp", cookies=_cookie(regular_user[1]))
    assert {s["name"] for s in resp.json()["servers"]} == {"confluence", "teamkey"}
    resp = await client.get("/api/admin/mcp-servers", cookies=_cookie(regular_user[1]))
    assert resp.status_code == 403, resp.text
    await client.patch(
        f"/api/admin/mcp-servers/{gid}", json={"enabled": False},
        cookies=_cookie(admin_user[1]),
    )
    resp = await client.get("/api/cabinet/mcp", cookies=_cookie(member_token))
    assert {s["name"] for s in resp.json()["servers"]} == {"confluence", "teamkey"}
    resp = await client.put(
        f"/api/cabinet/mcp/{gid}/secrets", json={"value": "x-12345678"},
        cookies=_cookie(member_token),
    )
    assert resp.status_code == 404, resp.text

    # The subject strips: deleting a group and demoting a moderator leave no
    # stale subject, and the next admin edit through the UI-shaped full PATCH
    # answers 200 (a stale subject would 422 unknown_subject).
    resp = await client.post(
        "/api/admin/mcp-servers",
        json=_def_body(
            name="multisubj",
            subjects=["public", "group:mcp-group-1", f"mod:{mod_uid}"],
        ),
        cookies=_cookie(admin_user[1]),
    )
    multi_id = resp.json()["id"]
    resp = await client.delete(
        "/api/admin/groups/mcp-group-1", cookies=_cookie(admin_user[1]),
    )
    assert resp.status_code == 200, resp.text
    row = await fetch_one("mcp_servers", multi_id)
    assert row["subjects"] == ["public", f"mod:{mod_uid}"], row["subjects"]
    resp = await client.patch(
        f"/api/admin/users/{mod_uid}", json={"role": "user"},
        cookies=_cookie(admin_user[1]),
    )
    assert resp.status_code == 200, resp.text
    row = await fetch_one("mcp_servers", multi_id)
    assert row["subjects"] == ["public"], row["subjects"]
    resp = await client.patch(
        f"/api/admin/mcp-servers/{multi_id}", json={
            "name": "multisubj", "title": row["title"], "url": row["url"],
            "auth_mode": row["auth_mode"], "auth_scheme": row["auth_scheme"],
            "subjects": row["subjects"],
        },
        cookies=_cookie(admin_user[1]),
    )
    assert resp.status_code == 200, resp.text


# ─── 3. The key flow → the render → the delete cascades ──────────────────────


# TEST-WHY: cross-process-contract — the write-only key contract end to end over real rows (cabinet PUT → user_secrets ciphertext → renderer → _build_turn_payload, nothing patched): the value never rides a response, the row is invisible to list_secrets and refused by http_request, only a resolved enabled host-bound key rides the turn, a keyless user pays no DNS, and the delete/mode cascades leave no orphaned ciphertext.
@pytest.mark.asyncio
async def test_key_flow_render_and_delete_cascades(
    client, test_db, project_with_doc, admin_user, monkeypatch, caplog,
):
    import mcp_servers as mcp
    from driver.client import _build_turn_payload

    await _wipe_servers(test_db)

    pin_tool_gates(monkeypatch, outbound_cidrs="127.0.0.0/8")
    pid, _idx, uid = project_with_doc  # the admin's own uid
    resp = await client.post(
        "/api/admin/mcp-servers", json=_def_body(), cookies=_cookie(admin_user[1]),
    )
    server_id = resp.json()["id"]
    cookie = _cookie(admin_user[1])
    user = {"user_id": uid, "role": "admin"}

    # No key yet → the server does not ride the payload, and it costs the
    # user NO DNS: the egress-refused warning fires only for a server whose
    # key resolved (the render resolves the key FIRST, dials second — with
    # the allowlist emptied, a keyed server warns and a keyless one does not).
    pin_tool_gates(monkeypatch, outbound_cidrs="")
    assert await mcp.render_for_user(test_db, user) == []
    with caplog.at_level("WARNING", logger="mcp_servers"):
        assert await mcp.render_for_user(test_db, user) == []
    assert not [r for r in caplog.records if "refused" in r.getMessage()]
    await client.put(
        f"/api/cabinet/mcp/{server_id}/secrets", json={"value": TOKEN_VALUE},
        cookies=cookie,
    )
    with caplog.at_level("WARNING", logger="mcp_servers"):
        assert await mcp.render_for_user(test_db, user) == []
    assert [r for r in caplog.records if "refused" in r.getMessage()], (
        "a keyed user's server must reach the egress check (and be skipped there)"
    )
    pin_tool_gates(monkeypatch, outbound_cidrs="127.0.0.0/8")

    # PUT stores (a pasted Bearer prefix is stripped by the shared gate); the
    # answer is the cabinet view, never the value; an under-8 key and a
    # non-printable value are refused without echoing values.
    resp = await client.put(
        f"/api/cabinet/mcp/{server_id}/secrets",
        json={"value": f" Bearer {TOKEN_VALUE} "},
        cookies=cookie,
    )
    assert resp.status_code == 200, resp.text
    assert TOKEN_VALUE not in resp.text
    entry = resp.json()["server"]
    assert entry["is_set"] is True and entry["needs_reenter"] is False
    assert entry["enabled"] is True
    for label, value in (
        ("under-8", "short-7"), ("non-printable", "bad\x01-key-9f13"),
    ):
        resp = await client.put(
            f"/api/cabinet/mcp/{server_id}/secrets", json={"value": value},
            cookies=cookie,
        )
        assert resp.status_code == 422, (label, resp.text)
        assert TOKEN_VALUE not in resp.text

    # The rendered payload entry: exact headers (ONE Bearer — the pasted
    # prefix was stripped) + the raw scrub list + the title; the field rides
    # _build_turn_payload verbatim and defaults to [].
    entries = await mcp.render_for_user(test_db, user)
    assert entries == [{
        "name": "confluence",
        "title": "Confluence, engineering",
        "url": "https://127.0.0.1:9301/mcp",
        "headers": {"Authorization": f"Bearer {TOKEN_VALUE}"},
        "secret_values": [TOKEN_VALUE],
        "auto_approve": False,
    }]
    assert _build_turn_payload(
        model="m", system_prompt="s", tools=[], agent_key="k", apply_mode="ask",
        mcp_servers=entries,
    )["mcp_servers"] == entries
    assert _build_turn_payload(
        model="m", system_prompt="s", tools=[], agent_key="k", apply_mode="ask",
    )["mcp_servers"] == []

    # The vault isolation: list_secrets does not see the server row;
    # http_request refuses it by name.
    token = await _make_internal_key(uid, pid)
    resp = await client.post(
        "/api/tool/list_secrets", json={}, headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200 and resp.json()["secrets"] == []
    resp = await client.post(
        "/api/tool/http_request",
        json={"secret": f"mcp--{server_id}", "url": "https://127.0.0.1:9301/mcp"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403, resp.text

    # Rename the server, create a NEW one under the old name, re-key: the
    # minted row name is server_id-derived, so no collision — the new key
    # lands and "Key saved" is true (the old name-based mint collided here).
    resp = await client.patch(
        f"/api/admin/mcp-servers/{server_id}", json={"name": "confluence-old"},
        cookies=cookie,
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        "/api/admin/mcp-servers", json=_def_body(name="confluence"),
        cookies=cookie,
    )
    assert resp.status_code in (200, 201), resp.text
    fresh_id = resp.json()["id"]
    resp = await client.put(
        f"/api/cabinet/mcp/{fresh_id}/secrets", json={"value": TOKEN_VALUE},
        cookies=cookie,
    )
    assert resp.status_code == 200 and resp.json()["server"]["is_set"] is True, resp.text
    rows = await test_db.query(
        "SELECT name, server_id FROM user_secrets WHERE server_id != NONE",
    )
    assert {r["name"] for r in rows} == {f"mcp--{fresh_id}", f"mcp--{server_id}"}, rows
    await client.delete(f"/api/admin/mcp-servers/{fresh_id}", cookies=cookie)

    # The toggle is disabled_by (all modes): declining drops the server from
    # the render; a re-entered key revives it. A keyless server (none mode)
    # is declinable too, and its schemas stop riding.
    resp = await client.patch(
        f"/api/cabinet/mcp/{server_id}", json={"enabled": False}, cookies=cookie,
    )
    assert resp.status_code == 200 and resp.json()["server"]["enabled"] is False
    assert await mcp.render_for_user(test_db, user) == []
    resp = await client.put(
        f"/api/cabinet/mcp/{server_id}/secrets", json={"value": TOKEN_VALUE},
        cookies=cookie,
    )
    assert resp.status_code == 200 and resp.json()["server"]["enabled"] is True
    resp = await client.post(
        "/api/admin/mcp-servers",
        json=_def_body(name="keyless", auth_mode="none"),
        cookies=cookie,
    )
    keyless_id = resp.json()["id"]
    names = {e["name"] for e in await mcp.render_for_user(test_db, user)}
    assert names == {"confluence-old", "keyless"}, names
    resp = await client.patch(
        f"/api/cabinet/mcp/{keyless_id}", json={"enabled": False}, cookies=cookie,
    )
    assert resp.status_code == 200, resp.text
    names = {e["name"] for e in await mcp.render_for_user(test_db, user)}
    assert names == {"confluence-old"}, names

    # The connection check classifier, offline arms only: the egress guard
    # refusing answers refused; a closed local port answers unreachable; the
    # answer carries no exception text. A personal server's admin check is
    # 422 (it is each user's to check).
    pin_tool_gates(monkeypatch, outbound_cidrs="")
    assert await mcp.check_connection(
        "https://127.0.0.1:9301/mcp", {},
    ) == {"status": "refused", "tools": None, "detail": None}
    pin_tool_gates(monkeypatch, outbound_cidrs="127.0.0.0/8")
    verdict = await mcp.check_connection("https://127.0.0.1:9391/mcp", {})
    assert verdict["status"] in ("unreachable", "protocol"), verdict
    assert verdict["tools"] is None
    resp = await client.post(
        f"/api/admin/mcp-servers/{server_id}/check", cookies=cookie,
    )
    assert resp.status_code == 422, resp.text
    resp = await client.post(
        f"/api/cabinet/mcp/{server_id}/check", cookies=cookie,
    )
    assert resp.status_code == 200 and resp.json()["status"] in (
        "unreachable", "protocol",
    ), resp.text

    # A url move to another host strands the stored key (the row's hosts
    # binding still names the OLD host): the server is out until the key is
    # re-entered, and the cabinet says so; re-entering restores the render
    # against the NEW host. Another user's chat carries nothing of mine.
    resp = await client.patch(
        f"/api/admin/mcp-servers/{server_id}",
        json={"url": "https://127.0.0.1:9302/mcp"}, cookies=cookie,
    )
    assert resp.status_code == 200, resp.text
    assert await mcp.render_for_user(test_db, user) == []
    resp = await client.get("/api/cabinet/mcp", cookies=cookie)
    entry = next(s for s in resp.json()["servers"] if s["id"] == server_id)
    assert entry["needs_reenter"] is True and entry["is_set"] is False
    await client.put(
        f"/api/cabinet/mcp/{server_id}/secrets",
        json={"value": TOKEN_VALUE}, cookies=cookie,
    )
    entries = await mcp.render_for_user(test_db, user)
    assert entries[0]["url"] == "https://127.0.0.1:9302/mcp"
    assert await mcp.render_for_user(test_db, {"user_id": "mcp-other-user", "role": "user"}) == []

    # A hidden server keeps its keyed user's DELETE (an orphaned ciphertext
    # is a credential left in the database): the cabinet shows it
    # unavailable, and the delete answers 200 with no visibility.
    resp = await client.patch(
        f"/api/admin/mcp-servers/{server_id}", json={"enabled": False}, cookies=cookie,
    )
    assert resp.status_code == 200, resp.text
    resp = await client.get("/api/cabinet/mcp", cookies=cookie)
    hidden = next(s for s in resp.json()["servers"] if s["id"] == server_id)
    assert hidden["unavailable"] is True and hidden["name"] == "confluence-old"
    assert hidden["host"] == "127.0.0.1:9302"
    resp = await client.put(
        f"/api/cabinet/mcp/{server_id}/secrets", json={"value": TOKEN_VALUE},
        cookies=cookie,
    )
    assert resp.status_code == 404, resp.text
    resp = await client.delete(f"/api/cabinet/mcp/{server_id}/secrets", cookies=cookie)
    assert resp.status_code == 200, resp.text
    assert await test_db.query(
        "SELECT * FROM user_secrets WHERE server_id = $sid", {"sid": server_id},
    ) == []
    await client.patch(
        f"/api/admin/mcp-servers/{server_id}", json={"enabled": True}, cookies=cookie,
    )

    # Mode changes drop credentials: personal → shared deletes every user's
    # rows; shared → personal clears the ciphertext. The delete cascades
    # (cabinet DELETE, admin server delete, moderator demote) leave no
    # orphaned ciphertext, and a deleted user leaves no disabled_by mark.
    resp = await client.patch(
        f"/api/admin/mcp-servers/{server_id}", json={"auth_mode": "shared"},
        cookies=cookie,
    )
    assert resp.status_code == 200, resp.text
    assert await test_db.query(
        "SELECT * FROM user_secrets WHERE server_id = $sid", {"sid": server_id},
    ) == []
    resp = await client.patch(
        f"/api/admin/mcp-servers/{server_id}",
        json={"auth_mode": "shared", "shared_key": TOKEN_VALUE}, cookies=cookie,
    )
    assert resp.status_code == 200 and resp.json()["shared_key_set"] is True
    resp = await client.patch(
        f"/api/admin/mcp-servers/{server_id}", json={"auth_mode": "none"},
        cookies=cookie,
    )
    assert resp.status_code == 200, resp.text
    row = await test_db.query(
        "SELECT shared_ciphertext, shared_host FROM type::record('mcp_servers', $id)",
        {"id": server_id},
    )
    assert row[0]["shared_ciphertext"] is None and row[0]["shared_host"] is None

    gone_uid = await _mk_user(test_db, "mcp-gone-user")
    await mcp.set_user_disabled(test_db, keyless_id, gone_uid, True)
    resp = await client.delete(
        f"/api/admin/users/{gone_uid}", cookies=_cookie(admin_user[1]),
    )
    assert resp.status_code == 200, resp.text
    row = await test_db.query(
        "SELECT disabled_by FROM type::record('mcp_servers', $id)", {"id": keyless_id},
    )
    # The deleted user's mark is gone; the admin's own earlier decline
    # (a live user's toggle) is untouched.
    assert row[0]["disabled_by"] == ["test-admin-001"], row[0]["disabled_by"]

    mod_uid = await _mk_user(test_db, "mcp-mod-3", role="moderator")
    mod_token = make_token(mod_uid, "mcp-mod-3", "moderator")
    resp = await client.post(
        "/api/admin/mcp-servers",
        json=_def_body(name="modserv2", subjects=[f"mod:{mod_uid}"]),
        cookies=_cookie(mod_token),
    )
    mod_server = resp.json()["id"]
    await client.put(
        f"/api/cabinet/mcp/{mod_server}/secrets",
        json={"value": TOKEN_VALUE}, cookies=_cookie(mod_token),
    )
    resp = await client.delete(
        f"/api/admin/mcp-servers/{server_id}", cookies=_cookie(admin_user[1]),
    )
    assert resp.status_code == 200, resp.text
    resp = await client.delete(
        f"/api/admin/mcp-servers/{keyless_id}", cookies=_cookie(admin_user[1]),
    )
    assert resp.status_code == 200, resp.text
    rows = await test_db.query("SELECT * FROM user_secrets")
    assert [r["name"] for r in rows] == [f"mcp--{mod_server}"], rows
    resp = await client.patch(
        f"/api/admin/users/{mod_uid}", json={"role": "user"},
        cookies=_cookie(admin_user[1]),
    )
    assert resp.status_code == 200, resp.text
    assert await test_db.query("SELECT * FROM mcp_servers") == []
    assert await test_db.query("SELECT * FROM user_secrets") == []

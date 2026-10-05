"""Model access by grants — public / moderator group / admin group (SYSTEM: model-access).

Every case drives the REFUSED principal through the real interface: the picker
(GET /api/chat/models), the turn (POST …/completions) and the admin routes.
A turn that PASSES the gate is observed as the 409 of a turn lock the test holds
itself — the lock is acquired after the gate, so 409 proves the gate let it by
and 403 model_forbidden proves it refused, with no harness involved.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from helpers import make_token, pin_chat_api
from password import hash_secret
from routes.admin_model_access import router as admin_access_router
from turn_lock import acquire_turn_lock, release_turn_lock

import config
from db import create_record

# The roster is the conftest autouse gateway pin (_pin_gateway_models); the ids
# below are its members, so the picker and the admin list read the real cache.
DEFAULT = "deepseek/flash"
PUBLIC = "gemini/pro"
MOD_MODEL = "local/orange/chat"
GROUP_MODEL = "local/orange/reasoner"
PRIVATE = "openai/luna"
SERVED = [DEFAULT, PUBLIC, MOD_MODEL, GROUP_MODEL, PRIVATE]


class _NoCapabilities:
    """The gateway client for /capabilities: an empty body = no reasoning map."""

    is_closed = False

    async def get(self, url: str, headers: dict | None = None):  # noqa: ARG002
        class _Resp:
            def json(self) -> dict:
                return {}

            def raise_for_status(self) -> None:
                return None
        return _Resp()


def _pin_default(monkeypatch, model: str) -> None:
    monkeypatch.setattr(config, "CHAT_MODEL", model)


@pytest.fixture
def gateway(monkeypatch, http_pool):
    """A configured gateway (pinned roster) and CHAT_MODEL = DEFAULT."""
    pin_chat_api(monkeypatch, url="http://gateway.example")
    _pin_default(monkeypatch, DEFAULT)
    http_pool("models_catalog", _NoCapabilities())


async def _user(uid: str, name: str, role: str, moderator_id: str | None = None) -> str:
    await create_record("users", uid, {
        "name": name, "email": f"{uid}@test.com", "password_hash": hash_secret("p"),
        "role": role, "user_facts": "", "moderator_id": moderator_id,
    })
    return make_token(uid, name, role, f"{uid}@test.com")


@pytest_asyncio.fixture
async def world(test_db, admin_user, project_with_doc):
    """Moderator M, member `red` (moderator_id=M), outsider `blue`; each user owns
    an AI chat session in the shared project."""
    pid, idx, _ = project_with_doc
    tokens = {
        "admin": admin_user[1],
        "mod": await _user("ma-mod", "Mod", "moderator"),
        "red": await _user("ma-red", "Red", "user", moderator_id="ma-mod"),
        "blue": await _user("ma-blue", "Blue", "user"),
    }
    sessions = {}
    for key, uid in (("mod", "ma-mod"), ("red", "ma-red"), ("blue", "ma-blue")):
        await create_record("project_members", f"pm-{uid}", {
            "project_id": pid, "user_id": uid, "access_level": "full",
        })
        sessions[key] = f"sess-{uid}"
        await create_record("chat_sessions", sessions[key], {
            "project_id": pid, "user_id": uid, "document_id": idx, "title": "",
        })
    return {"tokens": tokens, "sessions": sessions}


def _c(token: str) -> dict:
    return {"lore_session": token}


async def _picker(client, token: str) -> list[str]:
    resp = await client.get("/api/chat/models", cookies=_c(token))
    assert resp.status_code == 200, resp.text
    return resp.json()["models"]


async def _grant(client, world, model: str, subjects: list[str]):
    resp = await client.put(
        f"/api/admin/models/{model}", json={"subjects": subjects},
        cookies=_c(world["tokens"]["admin"]),
    )
    assert resp.status_code == 200, resp.text
    return resp


async def _turn(client, world, who: str, model: str | None, resolved: str | None = None) -> int:
    """POST a turn while holding the session's lock: 409 = gate passed, 403 = refused.
    `resolved` = the model the 403 must name when `model` is None (session-pinned)."""
    sid = world["sessions"][who]
    lock = await acquire_turn_lock(sid)
    try:
        resp = await client.post(
            f"/api/chat/sessions/{sid}/completions",
            json={"messages": [{"role": "user", "content": "hi"}], "model": model},
            cookies=_c(world["tokens"][who]),
        )
    finally:
        await release_turn_lock(sid, lock)
    if resp.status_code == 403:
        assert resp.json()["detail"] == {"code": "model_forbidden", "model": resolved or model}
    return resp.status_code


# ─── Picker + turn gate ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plain_user_sees_only_default_and_public(client, world, gateway):
    await _grant(client, world, PUBLIC, ["public"])
    assert await _picker(client, world["tokens"]["blue"]) == sorted([DEFAULT, PUBLIC])
    assert await _picker(client, world["tokens"]["admin"]) == sorted(SERVED)


@pytest.mark.asyncio
async def test_turn_on_private_model_is_refused(client, world, gateway):
    assert await _turn(client, world, "blue", PRIVATE) == 403
    assert await _turn(client, world, "blue", DEFAULT) == 409


@pytest.mark.asyncio
async def test_revoked_pinned_model_is_refused_on_next_turn(client, test_db, world, gateway):
    await test_db.query(
        "UPDATE type::record('chat_sessions', $id) SET model = $m",
        {"id": world["sessions"]["red"], "m": MOD_MODEL},
    )
    await _grant(client, world, MOD_MODEL, ["mod:ma-mod"])
    assert await _turn(client, world, "red", None) == 409
    await _grant(client, world, MOD_MODEL, [])
    assert await _turn(client, world, "red", None, resolved=MOD_MODEL) == 403


@pytest.mark.asyncio
async def test_default_model_needs_no_grant_and_follows_chat_model(
    client, world, gateway, monkeypatch,
):
    assert await _turn(client, world, "blue", DEFAULT) == 409
    _pin_default(monkeypatch, PRIVATE)
    assert await _turn(client, world, "blue", PRIVATE) == 409
    assert await _turn(client, world, "blue", DEFAULT) == 403


@pytest.mark.asyncio
async def test_moderator_group_member_gets_grants_and_loses_them(client, world, gateway):
    await _grant(client, world, MOD_MODEL, ["mod:ma-mod"])
    assert MOD_MODEL in await _picker(client, world["tokens"]["red"])
    assert MOD_MODEL in await _picker(client, world["tokens"]["mod"])
    assert MOD_MODEL not in await _picker(client, world["tokens"]["blue"])
    resp = await client.patch(
        "/api/admin/users/ma-red", json={"moderator_id": None},
        cookies=_c(world["tokens"]["admin"]),
    )
    assert resp.status_code == 200, resp.text
    assert MOD_MODEL not in await _picker(client, world["tokens"]["red"])
    assert await _turn(client, world, "red", MOD_MODEL) == 403


@pytest.mark.asyncio
async def test_admin_group_member_gets_the_union(client, world, gateway):
    admin = _c(world["tokens"]["admin"])
    gid = (await client.post("/api/admin/groups", json={"name": "Writers"}, cookies=admin)).json()["id"]
    resp = await client.put(f"/api/admin/groups/{gid}/members", json={"user_ids": ["ma-red"]}, cookies=admin)
    assert resp.status_code == 200, resp.text
    await _grant(client, world, GROUP_MODEL, [f"group:{gid}"])
    await _grant(client, world, MOD_MODEL, ["mod:ma-mod"])
    assert await _picker(client, world["tokens"]["red"]) == sorted([DEFAULT, MOD_MODEL, GROUP_MODEL])
    assert await _picker(client, world["tokens"]["blue"]) == [DEFAULT]


# ─── Admin surface ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_moderator_is_refused_on_every_admin_access_route(client, world):
    """Walks the router's own routes, so a route added later is covered too."""
    mod = _c(world["tokens"]["mod"])
    seen = 0
    for route in admin_access_router.routes:
        path = route.path.replace("{model_id:path}", "a/b").replace("{group_id}", "g1")
        for method in route.methods:
            body = {"subjects": [], "name": "n", "user_ids": []}
            resp = await client.request(method, path, json=body, cookies=mod)
            assert resp.status_code == 403, (method, path, resp.text)
            seen += 1
    assert seen >= 7


@pytest.mark.asyncio
async def test_members_on_virtual_moderator_group_is_404(client, world):
    resp = await client.put(
        "/api/admin/groups/mod:ma-mod/members", json={"user_ids": ["ma-red"]},
        cookies=_c(world["tokens"]["admin"]),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("subject", ["mod:ma-red", "mod:nobody", "group:missing", "everyone"])
async def test_unknown_subject_is_422(client, world, subject):
    resp = await client.put(
        f"/api/admin/models/{PRIVATE}", json={"subjects": [subject]},
        cookies=_c(world["tokens"]["admin"]),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["subjects"] == [subject]


@pytest.mark.asyncio
async def test_slashed_model_id_round_trips_and_shows_not_in_gateway(client, world, gateway):
    gone = "embeddings/giga/480m"
    resp = await _grant(client, world, gone, ["public"])
    assert resp.json() == {"id": gone, "subjects": ["public"]}
    rows = (await client.get("/api/admin/models", cookies=_c(world["tokens"]["admin"]))).json()
    row = next(r for r in rows if r["id"] == gone)
    assert row == {"id": gone, "in_gateway": False, "is_default": False, "subjects": ["public"]}
    assert next(r for r in rows if r["id"] == DEFAULT)["is_default"] is True


@pytest.mark.asyncio
async def test_revoked_then_regranted_is_a_fresh_row(client, test_db, world):
    async def row_ids() -> list:
        return await test_db.query(
            "SELECT VALUE id FROM model_grants WHERE model_id = $m", {"m": PUBLIC},
        )
    await _grant(client, world, PUBLIC, ["public"])
    first = await row_ids()
    await _grant(client, world, PUBLIC, ["public"])
    assert await row_ids() == first  # a kept subject keeps its row
    await _grant(client, world, PUBLIC, [])
    assert await row_ids() == []
    await _grant(client, world, PUBLIC, ["public"])
    again = await row_ids()
    assert len(again) == 1 and again != first


@pytest.mark.asyncio
async def test_group_delete_drops_members_and_grants(client, test_db, world):
    admin = _c(world["tokens"]["admin"])
    gid = (await client.post("/api/admin/groups", json={"name": "G"}, cookies=admin)).json()["id"]
    await client.put(f"/api/admin/groups/{gid}/members", json={"user_ids": ["ma-red"]}, cookies=admin)
    await _grant(client, world, GROUP_MODEL, [f"group:{gid}", "public"])
    resp = await client.delete(f"/api/admin/groups/{gid}", cookies=admin)
    assert resp.status_code == 200, resp.text
    assert await test_db.query("SELECT * FROM group_members WHERE group_id = $g", {"g": gid}) == []
    assert await test_db.query(
        "SELECT VALUE subject FROM model_grants WHERE model_id = $m", {"m": GROUP_MODEL},
    ) == ["public"]
    assert (await client.delete(f"/api/admin/groups/{gid}", cookies=admin)).status_code == 404


@pytest.mark.asyncio
async def test_groups_list_virtual_moderator_and_skips_soft_deleted(client, test_db, world, gateway):
    admin = _c(world["tokens"]["admin"])
    gid = (await client.post("/api/admin/groups", json={"name": "G"}, cookies=admin)).json()["id"]
    await client.put(
        f"/api/admin/groups/{gid}/members", json={"user_ids": ["ma-red", "ma-blue"]}, cookies=admin,
    )
    await _grant(client, world, MOD_MODEL, ["mod:ma-mod"])
    await test_db.query("UPDATE type::record('users', 'ma-blue') SET deleted_at = time::now()")
    groups = {g["id"]: g for g in (await client.get("/api/admin/groups", cookies=admin)).json()}
    assert [m["user_id"] for m in groups[gid]["members"]] == ["ma-red"]
    virtual = groups["mod:ma-mod"]
    assert virtual["kind"] == "moderator" and virtual["name"] == "Mod"
    assert [m["user_id"] for m in virtual["members"]] == ["ma-red"]
    assert virtual["models"] == [MOD_MODEL]


class _GatewayDown:
    """A gateway client whose every request fails — the outage the admin list survives."""

    is_closed = False

    async def get(self, url: str, headers: dict | None = None):  # noqa: ARG002
        raise httpx.ConnectError("gateway down")


@pytest.mark.asyncio
async def test_admin_models_list_survives_a_gateway_outage(client, monkeypatch, http_pool, world, gateway):
    """Grants stay listed (and so revocable) while the gateway is down; no row
    is claimed to be "not in gateway" — whether it is served is unknown."""
    import routes.chat.models_catalog as comp

    await _grant(client, world, PUBLIC, ["public"])
    monkeypatch.setattr(comp, "_gateway_models_cache", None)
    http_pool("models_catalog", _GatewayDown())
    resp = await client.get("/api/admin/models", cookies=_c(world["tokens"]["admin"]))
    assert resp.status_code == 200, resp.text
    assert resp.json() == [
        {"id": PUBLIC, "in_gateway": None, "is_default": False, "subjects": ["public"]},
    ]

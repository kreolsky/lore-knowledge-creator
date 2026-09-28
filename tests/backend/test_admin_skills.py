"""API tests for admin instance-skills endpoints — union list, override, tombstone, RBAC.

The endpoints are admin-ONLY (require_admin), same posture as admin_settings: a
moderator passes the user-manager gate but instance skills are a server-wide
act — every project's chat serves the same wire, and consumption is not
administration.
"""

import pytest
import pytest_asyncio
from agent_skills import frontmatter_name, shipped_skill_docs
from helpers import make_token
from password import hash_secret

from db import create_record


def _shipped_names() -> set[str]:
    """Frontmatter names of the repo's shipped skills (deterministic repo content)."""
    return {
        n for n in (
            frontmatter_name(d.get("content") or "")
            for d in shipped_skill_docs()
        ) if n
    }


def _any_shipped_name() -> str:
    shipped = _shipped_names()
    assert shipped, "the repo ships skill files — the union test needs one"
    return sorted(shipped)[0]


def _skill_md(name: str, body: str = "Instructions.", description: str = "d") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"


@pytest_asyncio.fixture(autouse=True)
async def _clean_instance_skills(test_db):
    """No instance_skills rows around every test."""
    await test_db.query("DELETE instance_skills")
    yield
    await test_db.query("DELETE instance_skills")


@pytest_asyncio.fixture
async def moderator_user(test_db):
    """A role='moderator' principal — passes require_user_manager_role, not require_admin."""
    uid = "test-mod-skills-001"
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})
    await create_record("users", uid, {
        "name": "testmod-skills",
        "email": "mod-skills@test.com",
        "password_hash": hash_secret("pass123"),
        "role": "moderator",
        "user_facts": "",
    })
    token = make_token(uid, "testmod-skills", "moderator", "mod-skills@test.com")
    yield uid, token
    await test_db.query("DELETE type::record('users', $id)", {"id": uid})


# ─── RBAC ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_regular_user_forbidden_everywhere(client, regular_user):
    """A role='user' principal gets 403 on GET, PUT and DELETE."""
    _, token = regular_user
    resp = await client.get("/api/admin/skills", cookies={"lore_session": token})
    assert resp.status_code == 403
    resp = await client.put(
        "/api/admin/skills/hello", json={"content": _skill_md("hello")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 403
    resp = await client.delete("/api/admin/skills/hello", cookies={"lore_session": token})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_moderator_forbidden_everywhere(client, moderator_user):
    """A moderator reaches /api/admin/users but NOT instance skills — a moderator
    still CONSUMES them in their projects' chats; that is not administration."""
    _, token = moderator_user
    resp = await client.get("/api/admin/skills", cookies={"lore_session": token})
    assert resp.status_code == 403
    resp = await client.put(
        "/api/admin/skills/hello", json={"content": _skill_md("hello")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 403
    resp = await client.delete("/api/admin/skills/hello", cookies={"lore_session": token})
    assert resp.status_code == 403


# ─── GET — the union ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_lists_shipped_and_instance_union(client, admin_user):
    """GET serves one row per DISTINCT name: shipped files (source=shipped,
    read-only) and instance rows (source=instance); an instance row shadows the
    shipped entry of the same name, and `shipped` still says a file exists
    (delete restores it)."""
    _, token = admin_user
    resp = await client.get("/api/admin/skills", cookies={"lore_session": token})
    assert resp.status_code == 200
    by_name = {s["name"]: s for s in resp.json()["skills"]}
    assert _shipped_names() <= set(by_name), "every shipped skill is listed"

    shipped_name = _any_shipped_name()
    entry = by_name[shipped_name]
    assert entry["source"] == "shipped"
    assert entry["shipped"] is True
    assert entry["enabled"] is True
    assert entry["content"] is None, "no instance row — nothing edited"
    assert entry["shipped_body"], "the shipped body rides for read-only view/override"

    # An instance skill joins the union as its own row.
    resp = await client.put(
        "/api/admin/skills/hello", json={"content": _skill_md("hello")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    resp = await client.get("/api/admin/skills", cookies={"lore_session": token})
    by_name = {s["name"]: s for s in resp.json()["skills"]}
    hello = by_name["hello"]
    assert hello["source"] == "instance"
    assert hello["shipped"] is False
    assert hello["enabled"] is True
    assert hello["content"] == _skill_md("hello")


# ─── PUT — create / override / tombstone ──────────────────────────────────────


@pytest.mark.asyncio
async def test_put_creates_instance_skill(client, admin_user, test_db):
    """PUT on a new name creates the instance row (UPSERT by name); the payload
    echoes the row."""
    content = _skill_md("hello", body="Hello instructions.")
    _, token = admin_user
    resp = await client.put(
        "/api/admin/skills/hello", json={"content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "hello"
    assert body["source"] == "instance"
    assert body["enabled"] is True
    assert body["content"] == content

    rows = await test_db.query(
        "SELECT content, enabled, updated_by FROM instance_skills WHERE name = $n",
        {"n": "hello"},
    )
    assert rows and rows[0]["content"] == content
    assert rows[0]["enabled"] is True
    assert rows[0]["updated_by"] == "test-admin-001"


@pytest.mark.asyncio
async def test_put_422_frontmatter_name_mismatch(client, admin_user):
    """The plugin overlays BY NAME, so a body whose frontmatter name differs
    from the path would be unreachable — refused loudly."""
    _, token = admin_user
    resp = await client.put(
        "/api/admin/skills/hello", json={"content": _skill_md("other")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_422_enabled_without_content(client, admin_user):
    """An enabled row with no content serves nothing and shadows nothing — the
    only honest contentless row is a tombstone (enabled=false) over a shipped
    skill."""
    _, token = admin_user
    resp = await client.put(
        "/api/admin/skills/hello", json={"enabled": True},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_422_tombstone_over_nothing(client, admin_user):
    """enabled=false with no content and no shipped skill of that name would
    suppress nothing — refused."""
    _, token = admin_user
    resp = await client.put(
        "/api/admin/skills/ghost-name", json={"enabled": False},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_override_shipped_copies_into_instance(client, admin_user, test_db):
    """PUT with content on a shipped name = the override: the instance row of
    the same name takes over (the file stays read-only repo content)."""
    name = _any_shipped_name()
    content = _skill_md(name, body="Overridden body.")
    _, token = admin_user
    resp = await client.put(
        f"/api/admin/skills/{name}", json={"content": content},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "instance"
    assert body["shipped"] is True, "delete still restores the shipped file"

    rows = await test_db.query(
        "SELECT content FROM instance_skills WHERE name = $n", {"n": name},
    )
    assert rows and rows[0]["content"] == content


@pytest.mark.asyncio
async def test_put_toggles_shipped_off_as_tombstone(client, admin_user, test_db):
    """PUT {enabled: false} on a shipped name (no content) creates the pure
    tombstone row: content stays null, the shipped skill is suppressed."""
    name = _any_shipped_name()
    _, token = admin_user
    resp = await client.put(
        f"/api/admin/skills/{name}", json={"enabled": False},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "instance"
    assert body["enabled"] is False
    assert body["content"] is None
    assert body["shipped_body"], "the UI still shows what is suppressed"

    rows = await test_db.query(
        "SELECT content, enabled FROM instance_skills WHERE name = $n", {"n": name},
    )
    assert rows and rows[0]["content"] is None and rows[0]["enabled"] is False


@pytest.mark.asyncio
async def test_put_partial_updates_keep_the_other_field(client, admin_user):
    """A content-only PUT keeps enabled; an enabled-only PUT keeps content —
    the UI edits one field at a time."""
    _, token = admin_user
    resp = await client.put(
        "/api/admin/skills/hello", json={"content": _skill_md("hello", body="v1")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    resp = await client.put(
        "/api/admin/skills/hello", json={"enabled": False},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == _skill_md("hello", body="v1")
    assert resp.json()["enabled"] is False
    resp = await client.put(
        "/api/admin/skills/hello",
        json={"content": _skill_md("hello", body="v2")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == _skill_md("hello", body="v2")
    assert resp.json()["enabled"] is False


# ─── DELETE — the row goes, the shipped file reappears ────────────────────────


@pytest.mark.asyncio
async def test_delete_removes_instance_row_only(client, admin_user):
    """DELETE removes the instance row (an override/tombstone/new skill); a
    shipped file of that name reappears untouched. No instance row → 404 —
    shipped files are repo content, not deletable here."""
    name = _any_shipped_name()
    _, token = admin_user
    resp = await client.put(
        f"/api/admin/skills/{name}", json={"enabled": False},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200

    resp = await client.delete(
        f"/api/admin/skills/{name}", cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json() == {"name": name, "reset": True}

    resp = await client.get("/api/admin/skills", cookies={"lore_session": token})
    entry = {s["name"]: s for s in resp.json()["skills"]}[name]
    assert entry["source"] == "shipped" and entry["enabled"] is True

    # Nothing left to delete — not the instance row, not the shipped file.
    resp = await client.delete(
        f"/api/admin/skills/{name}", cookies={"lore_session": token},
    )
    assert resp.status_code == 404
    resp = await client.delete(
        "/api/admin/skills/hello", cookies={"lore_session": token},
    )
    assert resp.status_code == 404


# ─── the wire — instance layers ride the turn payload ─────────────────────────


@pytest.mark.asyncio
async def test_wire_carries_instance_and_tombstones(
    client, admin_user, project_with_doc,
):
    """build_prompt_and_skill_docs adds the two instance layers: enabled rows
    WITH content as raw `instance` docs, disabled rows as the NAME list
    `instance_tombstones` (names, not bodies — the plugin's tombstone parse is
    body-based and an unparseable entry must suppress nothing)."""
    from agent_config import build_prompt_and_skill_docs, ensure_agent_system_docs

    pid = project_with_doc[0]
    await ensure_agent_system_docs(pid)
    tombstoned = _any_shipped_name()
    _, token = admin_user
    resp = await client.put(
        "/api/admin/skills/hello",
        json={"content": _skill_md("hello", body="Hello body.")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    # A disabled row WITH content is a stored edit + implicit tombstone: it
    # serves in neither layer except the tombstone name list.
    resp = await client.put(
        "/api/admin/skills/stored-edit",
        json={"content": _skill_md("stored-edit")},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    resp = await client.put(
        "/api/admin/skills/stored-edit", json={"enabled": False},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    resp = await client.put(
        f"/api/admin/skills/{tombstoned}", json={"enabled": False},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200

    _prompt, wire = await build_prompt_and_skill_docs(pid)
    instance = {d["location"]: d for d in wire["instance"]}
    assert "instance:hello" in instance
    assert instance["instance:hello"]["content"] == _skill_md("hello", body="Hello body.")
    assert "instance:stored-edit" not in instance, "disabled rows never serve"
    assert sorted(wire["instance_tombstones"]) == sorted(["stored-edit", tombstoned])
    # The pre-existing layers are untouched companions on the same wire.
    assert isinstance(wire["project"], list)
    assert wire["shipped"], "the shipped layer still rides (repo files)"
    assert isinstance(wire["tombstones"], list)

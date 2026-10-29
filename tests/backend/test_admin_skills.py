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
    assert "tombstones" not in wire, "a deleted project document suppresses nothing"

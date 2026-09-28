"""Tests for user preferences endpoints (per-project and global)."""

from __future__ import annotations


async def test_get_global_prefs_empty(client, admin_user):
    """GET /api/preferences/_global returns {} when nothing saved."""
    _, token = admin_user
    resp = await client.get(
        "/api/preferences/_global",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json() == {}


async def test_put_get_global_prefs(client, admin_user):
    """PUT then GET global prefs round-trips correctly."""
    _, token = admin_user
    prefs = {"theme": "dark", "panelWidths": {"left": 250, "right": 320}}

    resp = await client.put(
        "/api/preferences/_global",
        json={"preferences": prefs},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200

    resp = await client.get(
        "/api/preferences/_global",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json() == prefs


async def test_global_prefs_upsert_overwrites(client, admin_user):
    """Second PUT overwrites the first."""
    _, token = admin_user

    await client.put(
        "/api/preferences/_global",
        json={"preferences": {"theme": "light"}},
        cookies={"lore_session": token},
    )
    await client.put(
        "/api/preferences/_global",
        json={"preferences": {"theme": "dark", "panelWidths": {"left": 200, "right": 300}}},
        cookies={"lore_session": token},
    )

    resp = await client.get(
        "/api/preferences/_global",
        cookies={"lore_session": token},
    )
    data = resp.json()
    assert data["theme"] == "dark"
    assert data["panelWidths"] == {"left": 200, "right": 300}


async def test_project_prefs_with_search_query(client, admin_user, project_with_doc):
    """Per-project prefs round-trip with searchQuery field."""
    _, token = admin_user
    pid = project_with_doc[0]
    prefs = {
        "sidebarTab": "docs",
        "sidebarOpen": True,
        "rightPanelTab": "search",
        "rightPanelOpen": True,
        "searchQuery": "dragon lore",
    }

    resp = await client.put(
        f"/api/preferences/{pid}",
        json={"preferences": prefs},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200

    resp = await client.get(
        f"/api/preferences/{pid}",
        cookies={"lore_session": token},
    )
    assert resp.json()["searchQuery"] == "dragon lore"


async def test_global_prefs_isolation_between_users(client, admin_user, regular_user):
    """Two users get their own global prefs."""
    _, admin_token = admin_user
    _, user_token = regular_user

    await client.put(
        "/api/preferences/_global",
        json={"preferences": {"theme": "dark"}},
        cookies={"lore_session": admin_token},
    )
    await client.put(
        "/api/preferences/_global",
        json={"preferences": {"theme": "light"}},
        cookies={"lore_session": user_token},
    )

    resp_admin = await client.get(
        "/api/preferences/_global",
        cookies={"lore_session": admin_token},
    )
    resp_user = await client.get(
        "/api/preferences/_global",
        cookies={"lore_session": user_token},
    )

    assert resp_admin.json()["theme"] == "dark"
    assert resp_user.json()["theme"] == "light"


async def test_prefs_401_without_auth(client):
    """Both global and project endpoints reject unauthenticated requests."""
    resp = await client.get("/api/preferences/_global")
    assert resp.status_code == 401

    resp = await client.put(
        "/api/preferences/_global",
        json={"preferences": {"theme": "dark"}},
    )
    assert resp.status_code == 401

    resp = await client.get("/api/preferences/some-project-id")
    assert resp.status_code == 401


async def test_prefs_preserve_arbitrary_nested_object(client, admin_user):
    """An arbitrarily deep, heterogeneous preferences object survives a round-trip.

    Guards the 2026-07-28 schema-drift fix: `user_preferences` is SCHEMALESS, and its
    `preferences` field is declared `TYPE object` WITHOUT the `FLEXIBLE` keyword —
    FLEXIBLE is illegal on a SCHEMALESS table and made this DEFINE fail on every boot.
    Dropping the keyword is only correct if a plain `TYPE object` still preserves
    arbitrary children, which is what this asserts. See the ARCH block on
    `DEFINE TABLE user_preferences` in surreal/schema.surql.
    """
    _, token = admin_user
    # No None values: SurrealDB stores NONE as an ABSENT key, so a null would return
    # missing and say nothing about schema strictness — the thing under test.
    nested = {
        "documents": {
            "doc-uuid-1": {
                "mainEntity": {"id": "doc-uuid-1", "type": "document"},
                "rightPanelOpen": True,
                "rightPanelTab": "refs",
            },
        },
        "collapsedDocIds": ["a", "b"],
        "lastDocPosition": {"cursor": 0, "scroll": 12.5},
        "chatSessionByRef": {},
        "deep": {"one": {"two": {"three": [1, {"four": "x"}]}}},
    }

    resp = await client.put(
        "/api/preferences/_global",
        json={"preferences": nested},
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200

    resp = await client.get(
        "/api/preferences/_global",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200
    assert resp.json() == nested, "nested preferences were flattened or dropped"

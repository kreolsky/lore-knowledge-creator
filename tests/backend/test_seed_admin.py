"""seed_admin identifies the seeded admin by EMAIL, not by the editable name.

Why: an admin renamed in the cabinet (name 'finger', email still admin@lore.app) was
invisible to a name-keyed lookup; the re-CREATE hit the unique email index and failed
the whole boot — nobody could log in (dev incident 2026-09-18).
"""

import pytest
from password import hash_secret

from db import create_record, get_db
from migrations import seed_admin


@pytest.mark.asyncio
async def test_seed_admin_skips_renamed_admin(test_db, monkeypatch):
    monkeypatch.setenv("LORE_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("LORE_ADMIN_PASSWORD", "x")
    db = await get_db()
    await db.query("DELETE users WHERE email = 'admin@lore.app'")
    await create_record("users", "renamed-admin", {
        "name": "finger",
        "email": "admin@lore.app",
        "password_hash": hash_secret("x"),
        "role": "admin",
        "user_facts": "",
    })
    try:
        await seed_admin()  # must not raise, must not create a second row
        rows = await db.query("SELECT meta::id(id) AS id, name FROM users WHERE email = 'admin@lore.app'")
        assert [(r["id"], r["name"]) for r in rows] == [("renamed-admin", "finger")]
    finally:
        await db.query("DELETE users WHERE email = 'admin@lore.app'")


@pytest.mark.asyncio
async def test_seed_admin_creates_when_absent(test_db, monkeypatch):
    monkeypatch.setenv("LORE_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("LORE_ADMIN_PASSWORD", "x")
    db = await get_db()
    await db.query("DELETE users WHERE email = 'admin@lore.app'")
    try:
        await seed_admin()
        rows = await db.query("SELECT name, role FROM users WHERE email = 'admin@lore.app'")
        assert [(r["name"], r["role"]) for r in rows] == [("admin", "admin")]
    finally:
        await db.query("DELETE users WHERE email = 'admin@lore.app'")

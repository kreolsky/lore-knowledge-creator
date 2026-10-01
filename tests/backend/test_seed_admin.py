"""seed_admin bootstraps an admin only on an instance with no live admin.

Why: an admin renamed in the cabinet (name 'finger', email still admin@lore.app) was
invisible to a name-keyed lookup; the re-CREATE hit the unique email index and failed
the whole boot — nobody could log in (dev incident 2026-09-18). And a deleted admin
(delete releases the email) was re-created with the env password on every deploy.
"""

from contextlib import asynccontextmanager

import pytest
from password import hash_secret

from db import create_record, get_db
from migrations import seed_admin


@asynccontextmanager
async def _no_live_admins(db):
    """Demote every live admin of the shared session DB for the test, then restore."""
    ids = await db.query(
        "SELECT VALUE meta::id(id) FROM users WHERE role = 'admin' AND deleted_at IS NONE"
    )
    for uid in ids:
        await db.query("UPDATE type::record('users', $id) SET role = 'user'", {"id": uid})
    try:
        yield
    finally:
        for uid in ids:
            await db.query("UPDATE type::record('users', $id) SET role = 'admin'", {"id": uid})


def _env(monkeypatch):
    monkeypatch.setenv("LORE_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("LORE_ADMIN_PASSWORD", "x")


@pytest.mark.asyncio
async def test_seed_admin_skips_renamed_admin(test_db, monkeypatch):
    _env(monkeypatch)
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
async def test_seed_admin_creates_when_no_live_admin(test_db, monkeypatch):
    _env(monkeypatch)
    db = await get_db()
    await db.query("DELETE users WHERE email = 'admin@lore.app'")
    try:
        async with _no_live_admins(db):
            await seed_admin()
            rows = await db.query("SELECT name, role FROM users WHERE email = 'admin@lore.app'")
            assert [(r["name"], r["role"]) for r in rows] == [("admin", "admin")]
    finally:
        await db.query("DELETE users WHERE email = 'admin@lore.app'")


@pytest.mark.asyncio
async def test_seed_admin_does_not_resurrect_deleted_admin(test_db, monkeypatch):
    """The env admin was deleted in the UI (email released); another admin is live."""
    _env(monkeypatch)
    db = await get_db()
    await db.query("DELETE users WHERE email = 'admin@lore.app' OR email = 'other-admin@lore.app'")
    await create_record("users", "other-admin", {
        "name": "other",
        "email": "other-admin@lore.app",
        "password_hash": hash_secret("y"),
        "role": "admin",
        "user_facts": "",
    })
    try:
        await seed_admin()
        rows = await db.query("SELECT id FROM users WHERE email = 'admin@lore.app'")
        assert rows == []
    finally:
        await db.query("DELETE users WHERE email = 'admin@lore.app' OR email = 'other-admin@lore.app'")


@pytest.mark.asyncio
async def test_seed_admin_ignores_soft_deleted_admin(test_db, monkeypatch):
    """A soft-deleted admin tombstone does not count as a live admin."""
    _env(monkeypatch)
    db = await get_db()
    await db.query("DELETE users WHERE email = 'admin@lore.app' OR email = 'dead-admin@lore.app'")
    await create_record("users", "dead-admin", {
        "name": "dead",
        "email": "dead-admin@lore.app",
        "password_hash": hash_secret("y"),
        "role": "admin",
        "user_facts": "",
    })
    await db.query("UPDATE users:`dead-admin` SET deleted_at = time::now()")
    try:
        async with _no_live_admins(db):
            await seed_admin()
            rows = await db.query("SELECT role FROM users WHERE email = 'admin@lore.app'")
            assert [r["role"] for r in rows] == ["admin"]
    finally:
        await db.query("DELETE users WHERE email = 'admin@lore.app' OR email = 'dead-admin@lore.app'")

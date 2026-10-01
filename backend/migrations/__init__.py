"""Admin seeding — run once on app boot."""

import os
from uuid import uuid4

from password import hash_secret

from db import create_record, get_db


async def seed_admin() -> None:
    """Create the first admin user from env vars if no live admin exists yet."""
    username = os.environ.get("LORE_ADMIN_USERNAME")
    password = os.environ.get("LORE_ADMIN_PASSWORD")
    if not username or not password:
        return
    db = await get_db()
    # INVARIANT(security): the env admin is a bootstrap for an instance with NO live
    # admin — never a standing account re-created on every boot.
    # Why: deleting a user releases its email, so an email-keyed seed resurrected a
    # deleted admin with the CI-held password on every deploy (prod, 2026-09-29).
    live_admin = await db.query(
        "SELECT id FROM users WHERE role = 'admin' AND deleted_at IS NONE LIMIT 1"
    )
    if live_admin:
        return
    email = f"{username}@lore.app"
    # WHY: the seeded admin is looked up by EMAIL, never by name — the name is
    # editable in the admin cabinet; keying on it made a renamed admin invisible
    # to the seeder, whose re-CREATE then hit the unique email index and failed
    # the whole boot (nobody could log in).
    existing = await db.query("SELECT id FROM users WHERE email = $e LIMIT 1", {"e": email})
    if existing:
        return
    uid = str(uuid4())
    await create_record("users", uid, {
        "name": username,
        "email": email,
        "password_hash": hash_secret(password),
        "role": "admin",
        "user_facts": "",
    })
    print(f"[seed] Created admin user: {username}")

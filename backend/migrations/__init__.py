"""Admin seeding — run once on app boot."""

import os
import re
from uuid import uuid4

from password import hash_secret

from db import create_record, get_db

# INVARIANT: SurrealDB DEFINE USER embeds the username in SurrealQL.
# Only safe identifiers are allowed to prevent injection.  Why: DEFINE USER splices the username into SurrealQL verbatim; an unsafe name is injection into the schema layer, so only the safe-identifier regex is permitted.
_SAFE_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


async def seed_admin() -> None:
    """Create the first admin user from env vars if no admin exists yet."""
    username = os.environ.get("LORE_ADMIN_USERNAME")
    password = os.environ.get("LORE_ADMIN_PASSWORD")
    if not username or not password:
        return
    db = await get_db()
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


async def ensure_service_user() -> None:
    """Create a DB-level service user with OWNER role if SURREAL_SERVICE_USER is set.

    Production should set SURREAL_SERVICE_USER/SURREAL_SERVICE_PASS env vars
    and use them instead of root credentials for SURREAL_USER/SURREAL_PASS.
    """
    svc_user = os.environ.get("SURREAL_SERVICE_USER")
    svc_pass = os.environ.get("SURREAL_SERVICE_PASS")
    if not svc_user or not svc_pass:
        return
    # INVARIANT: validate service username before embedding in SurrealQL  Why: same injection guard as the DEFINE USER rule, applied at the service-user setup site before the name reaches SurrealQL.
    if not _SAFE_IDENTIFIER_RE.match(svc_user):
        raise ValueError(f"Invalid SURREAL_SERVICE_USER: {svc_user!r} — must be a safe identifier")
    db = await get_db()
    try:
        await db.query(
            f"DEFINE USER IF NOT EXISTS {svc_user} ON DATABASE PASSWORD $pwd ROLES OWNER",
            {"pwd": svc_pass},
        )
        print(f"[seed] Ensured DB service user: {svc_user}")
    except Exception as e:
        print(f"[ensure_service_user] Warning: {e}")

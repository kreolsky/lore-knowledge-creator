"""seed_dev must create documents through create_document so every non-reference
document carries a sort_key at birth — with no dependency on a boot-time repair sweep.

# SYSTEM: documents — covers the last create_record("documents") bypass (seed_dev).

Why: a raw create_record("documents", ...) leaves sort_key NONE, which filters the doc
out of its sibling group and breaks drag-reorder (incident 2026-06-04). seed_dev was the
last creation path bypassing documents.service.create_document; this test pins the closed
loop by driving the real seeding and asserting sort_key directly (no sweep).
"""

import pytest
from password import hash_secret

from db import create_record, get_db

# Tables seed_dev.main() writes; cleaned before AND after so the test is deterministic
# regardless of prior state in the shared per-worker DB.
_SEED_TABLES = [
    "document_history",
    "documents",
    "project_members",
    "projects",
    "users",
]


@pytest.mark.asyncio
async def test_seed_documents_carry_sort_key(test_db):
    """Every non-reference document created by seed_dev has a non-NONE sort_key.

    Before W6 seed_dev used raw create_record("documents", ...) with no sort_key, so
    this failed (rows with sort_key is None). After routing through create_document
    every non-reference doc is assigned a sort_key at creation.

    seed_dev.main() assumes admin@lore.app already exists (created at boot by seed_admin
    from LORE_ADMIN_USERNAME env); the test seeds it explicitly for isolation.
    """
    import seed_dev

    db = await get_db()
    # Deterministic slate + the admin user seed_dev's PROJECTS reference as owner/member.
    for table in _SEED_TABLES:
        try:
            await db.query(f"DELETE {table}")
        except Exception:
            pass
    admin_uid = "seed-test-admin"
    await create_record("users", admin_uid, {
        "name": "admin",
        "email": "admin@lore.app",
        "password_hash": hash_secret("x"),
        "role": "admin",
        "user_facts": "",
    })

    try:
        await seed_dev.main()
        rows = await db.query(
            "SELECT meta::id(id) AS id, title, sort_key FROM documents "
            "WHERE deleted_at IS NONE AND is_reference = false"
        )
        assert rows, "seed produced no non-reference documents"
        missing = [r for r in rows if r.get("sort_key") is None]
        assert not missing, (
            "seeded documents missing sort_key (would need the boot sweep): "
            f"{[r['title'] for r in missing]}"
        )
    finally:
        for table in _SEED_TABLES:
            try:
                await db.query(f"DELETE {table}")
            except Exception:
                pass

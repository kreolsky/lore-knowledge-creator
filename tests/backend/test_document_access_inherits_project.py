"""A document inherits its project's access level — per-document overrides removed.

Pins:
- a legacy `document_access` row (a DB migrated before the drop, before the
  migration ran) is ignored: the member resolves to project access, both when the
  row lowered and when it raised them;
- the refused principal: a `commentator` member holding a legacy `full` row gets
  403 on PATCH;
- the document_access_drop migration: logs the counts, drops per-document invites,
  dedups (email, project_id), narrows the unique index, drops the table; idempotent
  and a no-op on a fresh DB;
- DELETE /projects/{id}/invites with `document_id` is a 422, never a silent cancel
  of the project invite.
"""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest
import pytest_asyncio

from db import create_record
from migrations.migrate_document_access_drop import _migrate_document_access_drop

_LEGACY_SCHEMA = [
    "DEFINE TABLE IF NOT EXISTS document_access SCHEMAFULL",
    "DEFINE FIELD IF NOT EXISTS document_id ON document_access TYPE string",
    "DEFINE FIELD IF NOT EXISTS user_id ON document_access TYPE string",
    "DEFINE FIELD IF NOT EXISTS access_level ON document_access TYPE string",
    "DEFINE FIELD IF NOT EXISTS created_by ON document_access TYPE option<string>",
    "DEFINE FIELD IF NOT EXISTS document_id ON pending_invites TYPE option<string>",
    # The legacy three-field unique index is dropped so the test can seed the
    # duplicate (email, project_id) pair the migration must fold.
    "REMOVE INDEX IF EXISTS idx_pi_unique ON pending_invites",
]


async def _table_names(db) -> set[str]:
    info = await db.query("INFO FOR DB")
    blob = info[0] if isinstance(info, list) else info
    return set((blob or {}).get("tables") or {})


async def _pi_index(db) -> str:
    info = await db.query("INFO FOR TABLE pending_invites")
    blob = info[0] if isinstance(info, list) else info
    return (blob or {}).get("indexes", {}).get("idx_pi_unique", "")


@pytest_asyncio.fixture
async def legacy_schema(test_db):
    """Put the test DB in the pre-drop shape; restore the fresh shape afterwards."""
    for stmt in _LEGACY_SCHEMA:
        await test_db.query(stmt)
    yield test_db
    await test_db.query("DELETE pending_invites")
    await _migrate_document_access_drop(test_db)


async def _add_member(uid: str, pid: str, level: str) -> None:
    await create_record("project_members", str(uuid4()), {
        "project_id": pid, "user_id": uid, "access_level": level,
    })


async def _legacy_override(did: str, uid: str, level: str) -> None:
    await create_record("document_access", str(uuid4()), {
        "document_id": did, "user_id": uid, "access_level": level,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize(("project_level", "row_level"), [
    ("full", "readonly"),
    ("readonly", "full"),
])
async def test_legacy_override_row_resolves_to_project_access(
    legacy_schema, project_with_doc, regular_user, project_level, row_level,
):
    from access import get_document_access

    pid, did, _ = project_with_doc
    uid, _ = regular_user
    await _add_member(uid, pid, project_level)
    await _legacy_override(did, uid, row_level)

    assert await get_document_access(did, {"user_id": uid, "role": "user"}) == project_level


@pytest.mark.asyncio
async def test_commentator_with_legacy_full_row_cannot_patch(
    client, legacy_schema, project_with_doc, regular_user,
):
    pid, did, _ = project_with_doc
    uid, token = regular_user
    await _add_member(uid, pid, "commentator")
    await _legacy_override(did, uid, "full")

    resp = await client.patch(
        f"/api/documents/{did}", json={"title": "x"}, cookies={"lore_session": token},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_migration_drops_overrides_and_doc_invites_and_dedups(
    legacy_schema, project_with_doc, regular_user, caplog,
):
    db = legacy_schema
    pid, did, _ = project_with_doc
    uid, _ = regular_user
    await _legacy_override(did, uid, "readonly")
    await create_record("pending_invites", "pi-doc", {
        "email": "a@x.io", "project_id": pid, "document_id": did,
        "access_level": "full", "invited_by": uid,
    })
    await db.query(
        "CREATE pending_invites:pi_old SET email = 'b@x.io', project_id = $p, "
        "access_level = 'readonly', invited_by = $u, created_at = d'2026-01-01T00:00:00Z'",
        {"p": pid, "u": uid},
    )
    await db.query(
        "CREATE pending_invites:pi_new SET email = 'b@x.io', project_id = $p, "
        "access_level = 'full', invited_by = $u, created_at = d'2026-02-01T00:00:00Z'",
        {"p": pid, "u": uid},
    )

    with caplog.at_level(logging.INFO, logger="migrations.runner"):
        await _migrate_document_access_drop(db)

    assert "1 document_access row(s), 1 per-document pending invite(s)" in caplog.text
    assert "1 duplicate project invite(s) deleted" in caplog.text
    assert "document_access" not in await _table_names(db)
    rows = await db.query("SELECT meta::id(id) AS id, access_level FROM pending_invites")
    assert rows == [{"id": "pi_new", "access_level": "full"}]
    assert "FIELDS email, project_id UNIQUE" in await _pi_index(db)
    with pytest.raises(Exception, match="idx_pi_unique"):
        await db.query(
            "CREATE pending_invites SET email = 'b@x.io', project_id = $p, "
            "access_level = 'full', invited_by = $u",
            {"p": pid, "u": uid},
        )


@pytest.mark.asyncio
async def test_migration_is_a_noop_on_a_fresh_db_and_idempotent(test_db, caplog):
    before_tables = await _table_names(test_db)
    before_index = await _pi_index(test_db)

    with caplog.at_level(logging.INFO, logger="migrations.runner"):
        await _migrate_document_access_drop(test_db)
        await _migrate_document_access_drop(test_db)

    assert "document_access" not in before_tables
    assert await _table_names(test_db) == before_tables
    assert await _pi_index(test_db) == before_index
    assert caplog.text.count("0 document_access row(s), 0 per-document pending invite(s)") == 2


@pytest.mark.asyncio
async def test_cancel_invite_with_document_id_is_422(client, project_with_doc, admin_user, test_db):
    pid, did, _ = project_with_doc
    _, token = admin_user
    await create_record("pending_invites", "pi-keep", {
        "email": "c@x.io", "project_id": pid, "access_level": "full", "invited_by": "u",
    })

    resp = await client.delete(
        f"/api/projects/{pid}/invites",
        params={"email": "c@x.io", "document_id": did},
        cookies={"lore_session": token},
    )

    assert resp.status_code == 422, resp.text
    assert await test_db.query("SELECT email FROM pending_invites") == [{"email": "c@x.io"}]

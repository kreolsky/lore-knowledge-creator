"""API tests for GET /api/admin/info/storage — disk size + soft-deleted data.

Pins the admin-info plan's contract:
- tables are DERIVED from `INFO FOR DB`, never a literal list;
- a soft-deleted row raises `deleted_rows` (its own `deleted_at`);
- a LIVE row inside a soft-deleted project raises `in_deleted_projects_rows`
  (the buckets are disjoint: deleted = own deleted_at, any project state;
  in_deleted_projects = own live + project dead — the union is the plain sum);
- the disk numbers sum the real files; a missing DB mount is an explicit error (`disk: null` + `disk_error` naming
  the path), never 0 — no silent degradation.
"""

from pathlib import Path

import pytest

import config
from db import get_db


async def _storage(client, token: str) -> dict:
    resp = await client.get(
        "/api/admin/info/storage",
        cookies={"lore_session": token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _documents_row(data: dict) -> dict:
    return next(t for t in data["tables"] if t["name"] == "documents")


@pytest.mark.asyncio
async def test_storage_requires_admin(client, regular_user):
    """Non-admin → 403 (the refused principal)."""
    resp = await client.get(
        "/api/admin/info/storage",
        cookies={"lore_session": regular_user[1]},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_storage_admin_shape(client, admin_user, project_with_doc):
    """Admin → 200 with the full response shape."""
    data = await _storage(client, admin_user[1])
    assert "disk" in data
    assert "disk_error" in data
    assert "measured_ms" in data
    assert isinstance(data["tables"], list) and data["tables"]
    row = data["tables"][0]
    for key in ("name", "live_rows", "live_bytes", "deleted_rows", "deleted_bytes",
                "in_deleted_projects_rows", "in_deleted_projects_bytes"):
        assert key in row, f"table row missing {key}"
    totals = data["totals"]
    for key in ("live_rows", "live_bytes", "deleted_rows", "deleted_bytes",
                "in_deleted_projects_rows", "in_deleted_projects_bytes"):
        assert key in totals, f"totals missing {key}"


@pytest.mark.asyncio
async def test_tables_derived_from_info_for_db(client, admin_user, project_with_doc):
    """`tables` names equal `INFO FOR DB` tables — derived, not a literal list."""
    db = await get_db()
    info = await db.query("INFO FOR DB")
    blob = info[0] if isinstance(info, list) else info
    expected = set((blob or {}).get("tables") or {})
    assert expected, "test DB must have tables"

    data = await _storage(client, admin_user[1])
    assert {t["name"] for t in data["tables"]} == expected


@pytest.mark.asyncio
async def test_soft_deleted_document_raises_deleted_rows(client, admin_user, project_with_doc):
    """A soft-deleted document raises documents.deleted_rows by exactly 1."""
    _, token = admin_user
    _, idx_id, _ = project_with_doc
    db = await get_db()

    before = _documents_row(await _storage(client, token))

    await db.query(
        "UPDATE type::record('documents', $id) SET deleted_at = time::now()",
        {"id": idx_id},
    )
    after = _documents_row(await _storage(client, token))

    assert after["deleted_rows"] == before["deleted_rows"] + 1
    assert after["live_rows"] == before["live_rows"] - 1
    # The doc is soft-deleted while its project is LIVE: the in-deleted-projects
    # bucket must not move (the buckets are disjoint).
    assert after["in_deleted_projects_rows"] == before["in_deleted_projects_rows"]


@pytest.mark.asyncio
async def test_live_document_in_deleted_project_raises_in_dead_bucket(
    client, admin_user, project_with_doc,
):
    """A LIVE document inside a soft-deleted project raises
    documents.in_deleted_projects_rows by exactly 1 — project delete keeps the
    document's own deleted_at = NONE (projects.py delete_project), so the count
    must key on the PROJECT's state, not the document's."""
    _, token = admin_user
    pid, _idx_id, _ = project_with_doc
    db = await get_db()

    before = _documents_row(await _storage(client, token))

    await db.query(
        "UPDATE type::record('projects', $pid) SET deleted_at = time::now()",
        {"pid": pid},
    )
    after = _documents_row(await _storage(client, token))

    assert after["in_deleted_projects_rows"] == before["in_deleted_projects_rows"] + 1
    # The document itself is untouched: the own-deleted bucket must not move.
    assert after["deleted_rows"] == before["deleted_rows"]


_MISSING_DB_DIR = Path("/nonexistent-lore-info-test/lore.db")


def _mounted_db_dir(tmp_path: Path) -> Path:
    """lore.db with nested files (100 + 2000 B) plus 500 B beside it."""
    db_dir = tmp_path / "lore.db"
    (db_dir / "segments").mkdir(parents=True)
    (db_dir / "manifest").write_bytes(b"x" * 100)
    (db_dir / "segments" / "00001").write_bytes(b"x" * 2000)
    (tmp_path / "import.surql").write_bytes(b"x" * 500)
    return db_dir


@pytest.mark.asyncio
@pytest.mark.parametrize("mounted", [True, False], ids=["mounted", "missing"])
async def test_disk_part(client, admin_user, monkeypatch, tmp_path, mounted):
    """Mounted: db_bytes = files under lore.db (nested too); volume_bytes adds
    whatever sits beside it, so the gap an old copy or an export makes is visible.

    Missing: disk is null and disk_error names the path. A 0 next to real
    numbers reads as "empty database"; the mount being absent is a wiring
    failure and must surface as an error, distinct from a number.
    """
    db_dir = _mounted_db_dir(tmp_path) if mounted else _MISSING_DB_DIR
    monkeypatch.setattr(config, "SURREAL_DB_DIR", db_dir)

    data = await _storage(client, admin_user[1])
    if mounted:
        assert data["disk_error"] is None
        assert data["disk"] == {"db_bytes": 2100, "volume_bytes": 2600}
    else:
        assert data["disk"] is None
        assert str(_MISSING_DB_DIR) in data["disk_error"]

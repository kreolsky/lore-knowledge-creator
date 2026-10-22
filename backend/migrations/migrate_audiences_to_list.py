"""Migration: audiences_to_list — the audience trio folds into one list field.

# ARCH: a project's grants beyond its member rows live in ONE field,
#   `projects.audiences` = [{subject, access_level}] — 'public' (was is_public,
#   level readonly), a moderator group (was group_audience/group_access). The
#   mapping per row:
#     is_public        → {subject: 'public', access_level: 'readonly'}
#     group_audience   → {subject: <it>, access_level: group_access ?? 'readonly'}
#     neither          → []
#
# schema.surql carries the audiences DEFINEs but NO REMOVE for the trio: apply_schema
# runs BEFORE migrations (backend/main.py lifespan) — a schema-side REMOVE would drop
# the definitions before this migration copies the data (precedent and wording in
# migrate_document_access_drop). This module owns the REMOVEs.

INVARIANT(corruption): the row-value cleanup is a final `UPDATE … UNSET` AFTER the
REMOVEs, never before. Why: REMOVE FIELD drops the definition but leaves the stored
key on the row (verified on SurrealDB 2.x), and UNSET on the non-option is_public
fails coercion while its definition still stands — UNSET against an undefined field
strips the stray key.

Idempotent: the data pass skips rows that already carry audiences and no legacy
values; REMOVE … IF EXISTS is a no-op on a fresh DB (whose schema never defined
the trio — the INFO gate below skips the data pass entirely).
"""

from __future__ import annotations

from migrations._shared import logger


async def _projects_fields(db) -> dict:
    """The `fields` dict of INFO FOR TABLE projects ({} unreadable)."""
    info = await db.query("INFO FOR TABLE projects")
    blob = info[0] if isinstance(info, list) else info
    return (blob or {}).get("fields") or {}


async def _migrate_audiences_to_list(db) -> None:
    """Fold is_public/group_audience/group_access into audiences (idempotent)."""
    fields = await _projects_fields(db)
    legacy = [f for f in ("is_public", "group_audience", "group_access") if f in fields]
    if not legacy:
        # Fresh DB (or already-removed schema): nothing to fold, and the SELECT
        # below must not filter on undefined fields.
        return

    rows = await db.query(
        "SELECT meta::id(id) AS pid, is_public, group_audience, group_access, audiences "
        "FROM projects"
    )
    folded = skipped = 0
    for r in rows or []:
        if r.get("audiences") and not r.get("is_public") and not r.get("group_audience"):
            skipped += 1
            continue
        audiences: list[dict] = []
        if r.get("is_public"):
            audiences.append({"subject": "public", "access_level": "readonly"})
        if r.get("group_audience"):
            audiences.append({
                "subject": r["group_audience"],
                "access_level": r.get("group_access") or "readonly",
            })
        await db.query(
            "UPDATE type::record('projects', $pid) SET audiences = $audiences",
            {"pid": r["pid"], "audiences": audiences},
        )
        folded += 1
    logger.info(
        "audiences_to_list: %d project row(s) folded, %d already migrated (skipped)",
        folded, skipped,
    )
    # Definition removal FIRST (the UNSET of non-option is_public would fail
    # coercion while its definition stands), then the stray stored keys.
    await db.query("REMOVE INDEX IF EXISTS idx_projects_group_audience ON projects")
    for f in ("is_public", "group_audience", "group_access"):
        await db.query(f"REMOVE FIELD IF EXISTS {f} ON projects")
    await db.query("UPDATE projects UNSET is_public, group_audience, group_access")

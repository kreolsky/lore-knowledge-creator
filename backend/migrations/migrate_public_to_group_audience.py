"""Migration: public_to_group_audience — non-admin public projects become group audiences.

# ARCH: `is_public` became admin-only: a
#   non-admin's public project is re-expressed as a group audience — the same
#   group read (the owner's moderator group) minus the instance-wide broadcast.
#   Mapping per owner:
#     live non-admin with moderator_id → mod:<moderator_id>, readonly
#     live moderator                    → mod:<own uid>, readonly
#     soft-deleted owner / owner without a group / missing owner → private

# INVARIANT(persisted): an admin-owned public project is NEVER touched —
# public stays a legal, admin-only state. Why: rewriting it would silently
# narrow the audience; the migration converts only the states the new rules
# forbid.

Idempotent: a re-run finds no non-admin is_public=true row and does nothing.
"""

from __future__ import annotations

from migrations._shared import logger


def _owner_subject(owner: dict | None) -> str | None:
    """The moderator-group subject a public project folds to; None → private.

    Mapping per owner: live non-admin with moderator_id → mod:<moderator_id>;
    live moderator → mod:<own uid>; soft-deleted / groupless / missing → None.
    """
    if not owner or owner.get("deleted_at") is not None:
        return None
    if owner.get("moderator_id"):
        return f"mod:{owner['moderator_id']}"
    if owner.get("role") == "moderator":
        return f"mod:{owner['uid']}"
    return None


async def _migrate_public_to_group_audience(db) -> None:
    """Rewrite every non-admin `is_public = true` project per its owner's group."""
    # Fresh-DB boot after the audiences_to_list release: the schema no longer
    # defines is_public, and this body's SELECT must not filter on an undefined
    # field of a SCHEMAFULL table (whether SurrealDB errors there is unproven) —
    # with the definition gone, no row can hold the state, so skip entirely.
    info = await db.query("INFO FOR TABLE projects")
    blob = info[0] if isinstance(info, list) else info
    if "is_public" not in ((blob or {}).get("fields") or {}):
        return
    rows = await db.query(
        "SELECT meta::id(id) AS pid, owner_id FROM projects WHERE is_public = true"
    )
    if not rows:
        return
    owner_ids = list({r["owner_id"] for r in rows if r.get("owner_id")})
    owner_rows = await db.query(
        "SELECT meta::id(id) AS uid, role, moderator_id, deleted_at FROM users "
        "WHERE meta::id(id) IN $ids",
        {"ids": owner_ids},
    ) if owner_ids else []
    owners = {r["uid"]: r for r in (owner_rows or [])}

    to_audience = to_private = kept_admin = 0
    for r in rows:
        owner = owners.get(r.get("owner_id"))
        if owner and owner.get("role") == "admin":
            kept_admin += 1
            continue
        subject = _owner_subject(owner)
        if subject:
            await db.query(
                "UPDATE type::record('projects', $pid) SET is_public = false, "
                "group_audience = $ga, group_access = 'readonly'",
                {"pid": r["pid"], "ga": subject},
            )
            to_audience += 1
        else:
            await db.query(
                "UPDATE type::record('projects', $pid) SET is_public = false",
                {"pid": r["pid"]},
            )
            to_private += 1
    logger.info(
        "public_to_group_audience: %d project(s) → group audience, %d → private, "
        "%d admin-owned public kept",
        to_audience, to_private, kept_admin,
    )
